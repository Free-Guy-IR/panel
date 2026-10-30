import datetime
import importlib.util
import io
import sys
import types
import zipfile
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPTS = REPOSITORY / "scripts"
sys.path.insert(0, str(SCRIPTS))

DEBIAN_EMSIGN_FIXTURE = REPOSITORY / "tests/fixtures/debian-deb12u1-emsign-roots.crt"
DEBIAN_EMSIGN_FIXTURE_SHA256 = "322cb47a441d0ba379589490d45a3a59cbe6537d4a4d33b287c78455b38e01e8"
DEBIAN_EMSIGN_FIXTURE_SOURCE = (
    "the four emSign blocks, byte for byte and in bundle order, of /etc/ssl/certs/ca-certificates.crt in "
    "ghcr.io/free-guy-ir/panel:v5.34.24-usage-integrity (ca-certificates 20250419~deb12u1, bundle sha256 "
    "714d457d580922dbf1d0be8bd35ba236a842b50b0072ae791582a19adef772a5)"
)

OIDS = {
    "country": NameOID.COUNTRY_NAME,
    "unit": NameOID.ORGANIZATIONAL_UNIT_NAME,
    "organization": NameOID.ORGANIZATION_NAME,
    "common": NameOID.COMMON_NAME,
}


def load(name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_root(serial: int | None = None, **attributes: str) -> x509.Certificate:
    name = x509.Name([x509.NameAttribute(OIDS[key], value) for key, value in attributes.items()])
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.UTC)
    return (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(serial or x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )


def der(certificate: x509.Certificate) -> bytes:
    return certificate.public_bytes(serialization.Encoding.DER)


def pem(certificate: x509.Certificate) -> bytes:
    return certificate.public_bytes(serialization.Encoding.PEM)


def entry(label: str, certificate: x509.Certificate) -> bytes:
    subject = certificate.subject.rfc4514_string().encode("utf-8")
    return b"\n# Subject: " + subject + b'\n# Label: "' + label.encode() + b'"\n' + pem(certificate)


def clean_roots(count: int) -> list[x509.Certificate]:
    return [make_root(organization="Example Trust", common=f"Example Root {index}") for index in range(count)]


def distrusted_roots() -> dict[str, x509.Certificate]:
    return {
        "by-unit": make_root(country="IN", unit="emSign PKI", organization="Example", common="Root A"),
        "by-common-name": make_root(country="US", organization="Example", common="emSign ECC Root CA - C3"),
        "by-owner": make_root(country="US", organization="eMudhra Inc", common="Unrelated Root"),
        "by-owner-word": make_root(country="US", organization="emSign PKI Trust", common="Another Root"),
        "by-common-name-g9": make_root(country="US", organization="Example", common="eMudhra Root CA - G9"),
    }


def write_bundle(path: Path, kept: list[x509.Certificate], bad: dict[str, x509.Certificate]) -> bytes:
    content = b"".join(entry(f"kept-{index}", root) for index, root in enumerate(kept[:2]))
    content += b"".join(entry(label, root) for label, root in bad.items())
    content += b"".join(entry(f"kept-{index}", root) for index, root in enumerate(kept[2:], start=2))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content + b"\n")
    return content + b"\n"


def build_image_tree(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path / "image"
    bad = distrusted_roots()
    paths = {
        "certifi": root / "code/.venv/lib/python3.14/site-packages/certifi/cacert.pem",
        "venv-pip": root / "code/.venv/lib/python3.14/site-packages/pip/_vendor/certifi/cacert.pem",
        "unusual": root / "opt/x/roots.txt",
        "clean": root / "etc/clean.crt",
        "source": root / "code/scripts/tool.py",
        "excluded": root / "usr/share/ca-certificates/mozilla/emSign_Root_CA_-_G1.crt",
        "proc": root / "proc/1/environ",
        "archive": root / "usr/local/lib/python3.14/ensurepip/_bundled/pip-0-py3-none-any.whl",
    }
    write_bundle(paths["certifi"], clean_roots(3), {"g1": bad["by-unit"]})
    write_bundle(paths["venv-pip"], clean_roots(3), {"c1": bad["by-owner"]})
    write_bundle(paths["unusual"], clean_roots(2), {"g9": bad["by-common-name-g9"]})
    write_bundle(paths["clean"], clean_roots(2), {})
    paths["source"].parent.mkdir(parents=True)
    paths["source"].write_bytes(b'BLOCK = rb"-----BEGIN CERTIFICATE-----(.*?)-----END CERTIFICATE-----"\n')
    write_bundle(paths["excluded"], [], {"g1": bad["by-unit"]})
    write_bundle(paths["proc"], [], {"g1": bad["by-unit"]})
    paths["archive"].parent.mkdir(parents=True)
    with zipfile.ZipFile(paths["archive"], "w") as archive:
        archive.writestr("pip/_vendor/certifi/cacert.pem", pem(make_root(organization="Ok")) + pem(bad["by-owner"]))
    (root / "etc/link.pem").symlink_to(paths["unusual"])
    paths["root"] = root
    return paths


def excluded_for(root: Path) -> tuple[Path, ...]:
    return (root / "proc", root / "sys", root / "dev", root / "usr/share/ca-certificates")


def embedding_module(bad: x509.Certificate, good: x509.Certificate) -> bytes:
    return (
        b"from cryptography import x509\n"
        b'VENDOR_ROOT = b"""\n' + pem(bad) + b'"""\n'
        b'OTHER_ROOT = b"""\n' + pem(good) + b'"""\n'
        b"ROOTS = [x509.load_pem_x509_certificate(VENDOR_ROOT), x509.load_pem_x509_certificate(OTHER_ROOT)]\n"
    )


def zip_bytes(members: dict[str, bytes], compression: int = zipfile.ZIP_STORED) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=compression) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()
