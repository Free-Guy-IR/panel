import argparse
import io
import os
import ssl
import sys
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from cryptography import x509
from emsign_roots import (
    CERTIFICATE_TRAILER,
    DISCOVERY_ROOT,
    EXCLUDED_FROM_DISCOVERY,
    ZIP_MAGIC,
    certificate_ders,
    der_fingerprint,
    discover,
    is_archive_candidate,
    is_distrusted,
    is_distrusted_der,
    load_outbound_context_factory,
)

SYSTEM_BUNDLE = Path("/etc/ssl/certs/ca-certificates.crt")
SYSTEM_DIRECTORY = Path("/etc/ssl/certs")
MINIMUM_ROOTS = 1
MAXIMUM_ARCHIVE_DEPTH = 3


@dataclass
class Source:
    label: str
    location: str
    certificates: int = 0
    distrusted: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    minimum: int = 0

    def failures(self) -> list[str]:
        problems = [f"{self.label}: {error}" for error in self.errors]
        problems += [f"{self.label}: distrusted root {subject}" for subject in self.distrusted]
        if self.certificates < self.minimum:
            problems.append(f"{self.label}: only {self.certificates} certificates, expected at least {self.minimum}")
        return problems

    def worth_listing(self) -> bool:
        return bool(self.certificates or self.distrusted or self.errors or self.minimum)


def describe(der: bytes) -> str:
    try:
        return x509.load_der_x509_certificate(der).subject.rfc4514_string()
    except ValueError:
        return f"unparsed certificate sha256 {der_fingerprint(der)}"


def scan_bytes(source: Source, where: str, data: bytes) -> None:
    for der in certificate_ders(data):
        source.certificates += 1
        if is_distrusted_der(der):
            source.distrusted.append(f"{describe(der)} in {where}")


def scan_file(label: str, path: Path, minimum: int = 0) -> Source:
    source = Source(label, str(path), minimum=minimum)
    try:
        scan_bytes(source, str(path), path.read_bytes())
    except OSError as error:
        source.errors.append(f"cannot read {path}: {error}")
    return source


def scan_directory(label: str, directory: Path, minimum: int = 0) -> Source:
    source = Source(label, str(directory), minimum=minimum)
    files = 0
    for root, _, names in os.walk(directory):
        for name in sorted(names):
            entry = Path(root) / name
            target = entry.resolve()
            if not target.is_file():
                source.errors.append(f"{entry} points at {target}, which is not a file")
                continue
            data = target.read_bytes()
            if CERTIFICATE_TRAILER not in data:
                source.errors.append(f"{entry} holds no PEM certificate")
                continue
            files += 1
            scan_bytes(source, str(entry), data)
    if files < minimum:
        source.errors.append(f"only {files} readable files, expected at least {minimum}")
    return source


def unreadable_archive(where: str, reason: object) -> Source:
    return Source("archive", where, errors=[f"unreadable archive {where}: {reason}"])


def scan_member(where: str, content: bytes, depth: int, sources: list[Source]) -> None:
    if is_archive_candidate(content) and scan_archive_bytes(where, content, depth + 1, sources):
        return
    if CERTIFICATE_TRAILER in content:
        source = Source("archive", where)
        scan_bytes(source, where, content)
        sources.append(source)


def scan_archive_bytes(where: str, data: bytes, depth: int, sources: list[Source]) -> bool:
    prefixed = data.startswith(ZIP_MAGIC)
    try:
        bundle = zipfile.ZipFile(io.BytesIO(data))
        members = [member for member in bundle.infolist() if not member.is_dir()]
    except Exception as error:
        if prefixed:
            sources.append(unreadable_archive(where, repr(error)))
        return prefixed
    if depth > MAXIMUM_ARCHIVE_DEPTH:
        bundle.close()
        sources.append(unreadable_archive(where, f"zip nested deeper than {MAXIMUM_ARCHIVE_DEPTH} levels"))
        return True
    with bundle:
        for member in members:
            inner = f"{where}!{member.filename}"
            try:
                content = bundle.read(member)
            except Exception as error:
                sources.append(unreadable_archive(inner, repr(error)))
                continue
            scan_member(inner, content, depth, sources)
    return True


def scan_archive(archive: Path) -> list[Source]:
    sources: list[Source] = []
    try:
        data = archive.read_bytes()
    except OSError as error:
        return [unreadable_archive(str(archive), repr(error))]
    if not scan_archive_bytes(str(archive), data, 1, sources) and CERTIFICATE_TRAILER in data:
        source = Source("bundle", str(archive))
        scan_bytes(source, str(archive), data)
        sources.append(source)
    return sources


def scan_context(code_root: Path) -> Source:
    source = Source("outbound-context", "create_outbound_ssl_context().get_ca_certs()", minimum=MINIMUM_ROOTS)
    try:
        create_outbound_ssl_context = load_outbound_context_factory(code_root)
        certificates = create_outbound_ssl_context().get_ca_certs(binary_form=True)
    except Exception as error:
        source.errors.append(f"cannot build the outbound context: {error!r}")
        return source
    source.certificates = len(certificates)
    for der in certificates:
        try:
            certificate = x509.load_der_x509_certificate(der)
        except ValueError:
            if is_distrusted_der(der):
                source.distrusted.append(describe(der))
            continue
        if is_distrusted(certificate):
            source.distrusted.append(certificate.subject.rfc4514_string())
    return source


def run(code_root: Path, root: Path = DISCOVERY_ROOT, excluded: tuple[Path, ...] = EXCLUDED_FROM_DISCOVERY) -> int:
    import certifi

    certifi_path = Path(certifi.where())
    print(f"python {sys.version.split()[0]}, {ssl.OPENSSL_VERSION}")
    print(f"certifi {certifi.__version__} at {certifi_path}")
    print(f"default verify paths: {ssl.get_default_verify_paths()}")
    for name in ("SSL_CERT_FILE", "SSL_CERT_DIR"):
        print(f"{name}={os.environ.get(name, '')}")

    sources = [
        scan_file("certifi", certifi_path, MINIMUM_ROOTS),
        scan_file("system-bundle", SYSTEM_BUNDLE, MINIMUM_ROOTS),
        scan_directory("system-directory", SYSTEM_DIRECTORY, MINIMUM_ROOTS),
    ]
    seen = {certifi_path.resolve(), SYSTEM_BUNDLE.resolve()}
    started = time.monotonic()
    found = discover(root, excluded)
    for path in found.bundles:
        if path.resolve() in seen:
            continue
        seen.add(path.resolve())
        sources.append(scan_file("bundle", path))
    for archive in found.archives:
        sources += scan_archive(archive)
    if found.unreadable:
        sources.append(Source("discovery", str(root), errors=[f"unreadable: {item}" for item in found.unreadable]))
    elapsed = time.monotonic() - started
    sources.append(scan_context(code_root))

    failures = []
    listed = 0
    for source in sources:
        if source.worth_listing():
            listed += 1
            print(
                f"[{source.label}] {source.location}: {source.certificates} certificates, "
                f"{len(source.distrusted)} distrusted, {len(source.errors)} errors"
            )
        failures += source.failures()
    for failure in failures:
        print(f"FAIL {failure}")
    print(
        f"discovery under {root} (excluding {', '.join(str(path) for path in excluded)}): "
        f"{found.files} files, {found.bytes} bytes, {len(found.bundles)} with a certificate marker, "
        f"{len(found.archives)} zip archives, {len(found.unreadable)} unreadable, {elapsed:.1f}s; "
        f"{listed} sources hold certificates"
    )
    print("RESULT: PASS" if not failures else f"RESULT: FAIL ({len(failures)} problems)")
    return 0 if not failures else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--code-root", type=Path, default=Path("/code"))
    return run(parser.parse_args().code_root)


if __name__ == "__main__":
    sys.exit(main())
