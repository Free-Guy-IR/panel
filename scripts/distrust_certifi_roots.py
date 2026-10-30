import base64
import importlib
import os
import re
import sys
import tempfile
from pathlib import Path

from cryptography import x509
from emsign_roots import (
    DISCOVERY_ROOT,
    DISTRUSTED_FINGERPRINTS,
    EXCLUDED_FROM_DISCOVERY,
    der_fingerprint,
    discover,
    holds_distrusted,
    is_distrusted,
    is_distrusted_der,
)

BEGIN_MARKER = b"-----BEGIN CERTIFICATE-----"
END_MARKER = b"-----END CERTIFICATE-----"
ANY_MARKER = re.compile(rb"-----(BEGIN|END) ")
BLOCK = re.compile(rb"-----BEGIN CERTIFICATE-----(.*?)-----END CERTIFICATE-----", re.DOTALL)
BYTE_ORDER_MARKS = (b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")
MAXIMUM_DISTINCT_REMOVED = len(DISTRUSTED_FINGERPRINTS)
IMAGE_FLAG = "--image"
IMAGE_BUILD_VARIABLE = "PG_IMAGE_BUILD"
SOURCE_SUFFIXES = (".py", ".pyc", ".pyo", ".pyw")
HUMAN_DECISION = "a human must decide: remove the root from that file, or drop the file from the image"


class BundleError(ValueError):
    pass


def line_body(line: bytes) -> bytes:
    return line.rstrip(b"\r\n")


def decode_block(lines: list[bytes]) -> bytes:
    body = b"".join(line_body(line) for line in lines[1:-1])
    try:
        return base64.b64decode(body, validate=True)
    except ValueError as error:
        raise BundleError(f"a certificate block is not valid base64: {error}") from error


def split_entries(data: bytes) -> tuple[list[tuple[bytes, bytes, bytes]], bytes]:
    if data.startswith(BYTE_ORDER_MARKS):
        raise BundleError("the bundle starts with a byte order mark")
    entries = []
    pending: list[bytes] = []
    block: list[bytes] = []
    for line in data.splitlines(keepends=True):
        body = line_body(line)
        if body == BEGIN_MARKER:
            if block:
                raise BundleError("a certificate block opens inside another one")
            block = [line]
        elif block:
            if ANY_MARKER.search(body) and body != END_MARKER:
                raise BundleError(f"unexpected marker inside a certificate block: {body[:60]!r}")
            block.append(line)
            if body == END_MARKER:
                entries.append((b"".join(pending), b"".join(block), decode_block(block)))
                pending = []
                block = []
        else:
            if ANY_MARKER.search(body):
                raise BundleError(f"a PEM marker is not on a line of its own or is not a certificate: {body[:60]!r}")
            pending.append(line)
    if block:
        raise BundleError("the last certificate block is never closed")
    if data.count(b"-----BEGIN ") != len(entries) or data.count(b"-----END ") != len(entries):
        raise BundleError("the bundle holds PEM markers outside the certificate blocks that were read")
    return entries, b"".join(pending)


def entry_is_distrusted(der: bytes) -> bool:
    try:
        certificate = x509.load_der_x509_certificate(der)
    except ValueError:
        return is_distrusted_der(der)
    return is_distrusted(certificate)


def verify_written(data: bytes, expected: int) -> None:
    try:
        certificates = x509.load_pem_x509_certificates(data)
        found = len(certificates)
        distrusted = sum(1 for certificate in certificates if is_distrusted(certificate))
    except ValueError:
        blocks = [base64.b64decode(b"".join(match.split()), validate=True) for match in BLOCK.findall(data)]
        found = len(blocks)
        distrusted = sum(1 for der in blocks if entry_is_distrusted(der))
    if distrusted:
        raise RuntimeError(f"the rewritten bundle still holds {distrusted} distrusted root(s)")
    if found != expected:
        raise RuntimeError(f"the rewritten bundle holds {found} certificates, expected {expected}")
    if found == 0:
        raise RuntimeError("the rewritten bundle holds no certificate")


def write_atomically(path: Path, content: bytes) -> None:
    status = path.stat()
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chown(temporary, status.st_uid, status.st_gid)
        os.chmod(temporary, status.st_mode & 0o7777)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def first_foreign_line(data: bytes) -> tuple[int, bytes] | None:
    inside = False
    for number, line in enumerate(data.splitlines(), start=1):
        body = line.rstrip(b"\r")
        if body == BEGIN_MARKER:
            inside = True
        elif body == END_MARKER:
            inside = False
        elif not inside and body.strip() and not body.startswith(b"#"):
            return number, body
    return None


def require_genuine_bundle(target: Path, original: bytes) -> None:
    if target.suffix in SOURCE_SUFFIXES:
        raise BundleError(f"{target} is Python source or bytecode, not a certificate bundle; {HUMAN_DECISION}")
    foreign = first_foreign_line(original)
    if foreign is not None:
        number, body = foreign
        raise BundleError(
            f"{target} is not a certificate bundle: line {number} ({body[:60]!r}) is neither blank nor a # comment; "
            f"{HUMAN_DECISION}"
        )


class Plan:
    def __init__(self, target: Path, content: bytes, expected: int, removed: list[str]) -> None:
        self.target = target
        self.content = content
        self.expected = expected
        self.removed = removed


def plan_strip(path: Path, genuine_only: bool = True) -> Plan:
    target = Path(path).resolve(strict=True)
    if genuine_only and target.suffix in SOURCE_SUFFIXES:
        require_genuine_bundle(target, b"")
    original = target.read_bytes()
    entries, trailer = split_entries(original)
    if not entries:
        raise BundleError(f"{target} holds no certificate")
    kept = []
    removed = []
    fingerprints = set()
    for header, block, der in entries:
        if entry_is_distrusted(der):
            fingerprints.add(der_fingerprint(der))
            try:
                removed.append(x509.load_der_x509_certificate(der).subject.rfc4514_string())
            except ValueError:
                removed.append(f"unparsed certificate with a distrusted fingerprint ({len(der)} bytes)")
        else:
            kept.append(header + block)
    if removed and genuine_only:
        require_genuine_bundle(target, original)
    if len(fingerprints) > MAXIMUM_DISTINCT_REMOVED:
        raise BundleError(
            f"{target} holds {len(fingerprints)} distinct distrusted roots, more than the {MAXIMUM_DISTINCT_REMOVED} "
            "known emSign roots: a new emSign or eMudhra root has appeared; review it and add its fingerprint to "
            "scripts/emsign_roots.py; nothing written"
        )
    if removed and (not kept or len(kept) != len(entries) - len(removed)):
        raise BundleError(f"{target} would keep {len(kept)} of {len(entries)} certificates; nothing written")
    return Plan(target, b"".join(kept) + trailer, len(entries) - len(removed), removed)


def apply_plan(plan: Plan) -> None:
    if not plan.removed:
        return
    write_atomically(plan.target, plan.content)
    written = plan.target.read_bytes()
    if written != plan.content:
        raise RuntimeError(f"{plan.target} does not hold the bytes that were written")
    verify_written(written, plan.expected)


def strip_bundle(path: Path, genuine_only: bool = True) -> list[str]:
    plan = plan_strip(path, genuine_only)
    apply_plan(plan)
    return plan.removed


def certifi_bundle() -> Path:
    certifi = importlib.import_module("certifi")
    path = Path(certifi.where())
    if not path.is_file():
        raise FileNotFoundError(f"certifi.where() names {path}, which is not a file")
    return path


def discovered_targets(root: Path, excluded: tuple[Path, ...]) -> list[Path]:
    targets = []
    for path in discover(root, excluded).bundles:
        try:
            if holds_distrusted(path.read_bytes()):
                targets.append(path)
        except OSError:
            continue
    return targets


def image_targets(root: Path | None = None, excluded: tuple[Path, ...] | None = None) -> list[Path]:
    targets = [certifi_bundle()]
    seen = {targets[0].resolve()}
    root = DISCOVERY_ROOT if root is None else root
    excluded = EXCLUDED_FROM_DISCOVERY if excluded is None else excluded
    for path in discovered_targets(root, excluded):
        if path.resolve() not in seen:
            seen.add(path.resolve())
            targets.append(path)
    return targets


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == [IMAGE_FLAG] and os.environ.get(IMAGE_BUILD_VARIABLE) != "1":
        print(
            f"refusing {IMAGE_FLAG}: it rewrites trust bundles across the whole filesystem and runs only in the image "
            f"build, which sets {IMAGE_BUILD_VARIABLE}=1",
            file=sys.stderr,
        )
        return 2
    try:
        if arguments == [IMAGE_FLAG]:
            paths = image_targets()
            exempt = {paths[0].resolve()}
        elif arguments:
            paths = [Path(argument) for argument in arguments]
            exempt = set()
        else:
            paths = [certifi_bundle()]
            exempt = {paths[0].resolve()}
        for path in paths:
            if not path.is_file():
                raise FileNotFoundError(f"{path} is not a file")
    except (ImportError, OSError) as error:
        print(f"cannot find the bundle: {error}", file=sys.stderr)
        return 1
    plans = []
    for path in paths:
        try:
            plans.append(plan_strip(path, genuine_only=path.resolve() not in exempt))
        except Exception as error:
            print(f"{path}: refusing the bundle, nothing was written: {error}", file=sys.stderr)
            return 1
    for plan in plans:
        try:
            apply_plan(plan)
        except Exception as error:
            print(f"{plan.target}: the rewrite failed: {error}", file=sys.stderr)
            return 1
        print(f"{plan.target}: removed {len(plan.removed)} distrusted root(s)")
        for subject in plan.removed:
            print(f"  {subject}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
