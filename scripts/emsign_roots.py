import base64
import hashlib
import importlib.util
import io
import os
import re
import stat
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import BinaryIO

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import NameOID

DISTRUSTED_FINGERPRINTS = {
    "40f6af0346a99aa1cd1d555a4e9cce62c7f9634603ee406615833dc8c8d00367": "emSign Root CA - G1",
    "86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b": "emSign ECC Root CA - G3",
    "125609aa301da0a249b97a8239cb6a34216f44dcac9f3954b14292f2e8c8608f": "emSign Root CA - C1",
    "bc4d809b15189d78db3e1d8cf4f9726a795da1643ca5f1358e1ddb0edc0d7eb3": "emSign ECC Root CA - C3",
}
DISTRUSTED_TOKEN = re.compile(r"(?<![0-9a-z])(?:emsign|emudhra)(?![0-9a-z])", re.IGNORECASE)
NAME_ATTRIBUTES = (NameOID.ORGANIZATION_NAME, NameOID.ORGANIZATIONAL_UNIT_NAME, NameOID.COMMON_NAME)

CERTIFICATE_TRAILER = b"CERTIFICATE-----"
ANY_CERTIFICATE_BLOCK = re.compile(
    rb"-----BEGIN (CERTIFICATE|TRUSTED CERTIFICATE|X509 CERTIFICATE)-----(.*?)-----END \1-----", re.DOTALL
)
ZIP_MAGIC = b"PK\x03\x04"
DISCOVERY_CHUNK = 1 << 20
DISCOVERY_ROOT = Path("/")
EXCLUDED_FROM_DISCOVERY = (Path("/proc"), Path("/sys"), Path("/dev"), Path("/usr/share/ca-certificates"))


def der_fingerprint(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


def names_a_distrusted_owner(name: x509.Name) -> bool:
    return any(
        DISTRUSTED_TOKEN.search(str(attribute.value))
        for oid in NAME_ATTRIBUTES
        for attribute in name.get_attributes_for_oid(oid)
    )


def is_distrusted(certificate: x509.Certificate) -> bool:
    der = certificate.public_bytes(serialization.Encoding.DER)
    return (
        der_fingerprint(der) in DISTRUSTED_FINGERPRINTS
        or names_a_distrusted_owner(certificate.subject)
        or names_a_distrusted_owner(certificate.issuer)
    )


def is_distrusted_der(der: bytes) -> bool:
    if der_fingerprint(der) in DISTRUSTED_FINGERPRINTS:
        return True
    try:
        certificate = x509.load_der_x509_certificate(der)
    except ValueError:
        return False
    return is_distrusted(certificate)


def leading_der(blob: bytes) -> bytes | None:
    if len(blob) < 2 or blob[0] != 0x30:
        return None
    length = blob[1]
    offset = 2
    if length & 0x80:
        count = length & 0x7F
        if count == 0 or count > 4 or len(blob) < 2 + count:
            return None
        length = int.from_bytes(blob[2 : 2 + count], "big")
        offset = 2 + count
    end = offset + length
    return blob[:end] if end <= len(blob) else None


def certificate_ders(data: bytes) -> Iterator[bytes]:
    for _, body in ANY_CERTIFICATE_BLOCK.findall(data):
        try:
            blob = base64.b64decode(b"".join(body.split()), validate=True)
        except ValueError:
            continue
        der = leading_der(blob)
        if der is not None:
            yield der


def holds_distrusted(data: bytes) -> bool:
    return any(is_distrusted_der(der) for der in certificate_ders(data))


def is_archive_candidate(data: bytes) -> bool:
    if data.startswith(ZIP_MAGIC):
        return True
    try:
        return zipfile.is_zipfile(io.BytesIO(data))
    except Exception:
        return False


def is_archive_file(handle: BinaryIO) -> bool:
    handle.seek(0)
    if handle.read(len(ZIP_MAGIC)) == ZIP_MAGIC:
        return True
    try:
        return zipfile.is_zipfile(handle)
    except Exception:
        return False


def holds_marker(handle: BinaryIO, chunk_size: int) -> bool:
    overlap = len(CERTIFICATE_TRAILER) - 1
    handle.seek(0)
    tail = b""
    while chunk := handle.read(chunk_size):
        window = tail + chunk
        if CERTIFICATE_TRAILER in window:
            return True
        tail = window[-overlap:]
    return False


def is_excluded(path: Path, excluded: tuple[Path, ...]) -> bool:
    return any(path == root or root in path.parents for root in excluded)


class Discovery:
    def __init__(self) -> None:
        self.bundles: list[Path] = []
        self.archives: list[Path] = []
        self.unreadable: list[str] = []
        self.files = 0
        self.bytes = 0


def discover(root: Path = DISCOVERY_ROOT, excluded: tuple[Path, ...] = EXCLUDED_FROM_DISCOVERY) -> Discovery:
    found = Discovery()

    def record_error(error: OSError) -> None:
        found.unreadable.append(f"{error.filename}: {error.strerror}")

    for directory, subdirectories, names in os.walk(root, onerror=record_error):
        here = Path(directory)
        subdirectories[:] = sorted(name for name in subdirectories if not is_excluded(here / name, excluded))
        for name in sorted(names):
            path = here / name
            try:
                if not stat.S_ISREG(os.lstat(path).st_mode):
                    continue
                with open(path, "rb") as handle:
                    size = os.fstat(handle.fileno()).st_size
                    archive = is_archive_file(handle)
                    bundle = not archive and holds_marker(handle, DISCOVERY_CHUNK)
            except OSError as error:
                found.unreadable.append(f"{path}: {error.strerror}")
                continue
            found.files += 1
            found.bytes += size
            if archive:
                found.archives.append(path)
            elif bundle:
                found.bundles.append(path)
    return found


def load_outbound_context_factory(code_root: Path) -> Callable:
    path = code_root / "app" / "utils" / "ssl_context.py"
    spec = importlib.util.spec_from_file_location("pg_image_check_ssl_context", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.create_outbound_ssl_context
