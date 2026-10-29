import argparse
import datetime
import ipaddress
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from emsign_roots import is_distrusted, load_outbound_context_factory

CA_CONFIGURATION = Path("/etc/ca-certificates.conf")
MOZILLA_DIRECTORY = Path("/usr/share/ca-certificates/mozilla")
SYSTEM_BUNDLE = Path("/etc/ssl/certs/ca-certificates.crt")
SYSTEM_DIRECTORY = Path("/etc/ssl/certs")
UNABLE_TO_GET_LOCAL_ISSUER = 20
THROWAWAY_FLAG = "PG_THROWAWAY_CONTAINER"
CONTAINER_MARKERS = (Path("/.dockerenv"), Path("/run/.containerenv"))
OUTBOUND_CONTEXT = {}
DOCKERFILE_DESELECT_EXPRESSION = r"s@^(mozilla/(emSign|eMudhra)[^/]*\.crt)$@!\1@"
EMSIGN_SUBJECTS = {
    "emSign Root CA - G1": ("IN", "eMudhra Technologies Limited", "rsa"),
    "emSign ECC Root CA - G3": ("IN", "eMudhra Technologies Limited", "ec"),
    "emSign Root CA - C1": ("US", "eMudhra Inc", "rsa"),
    "emSign ECC Root CA - C3": ("US", "eMudhra Inc", "ec"),
}


def emsign_subject(common_name: str) -> x509.Name:
    country, organization, _ = EMSIGN_SUBJECTS[common_name]
    return x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, country),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "emSign PKI"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, organization),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ]
    )


class Outcome:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def check(self, label: str, passed: bool, detail: str) -> None:
        print(f"{'PASS' if passed else 'FAIL'} {label}: {detail}")
        if not passed:
            self.failures.append(label)


def validity() -> tuple[datetime.datetime, datetime.datetime]:
    now = datetime.datetime.now(datetime.UTC)
    return now - datetime.timedelta(hours=1), now + datetime.timedelta(days=1)


def make_root(
    subject: x509.Name, key: ec.EllipticCurvePrivateKey | rsa.RSAPrivateKey, digest: hashes.HashAlgorithm
) -> x509.Certificate:
    not_before, not_after = validity()
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, digest)
    )


def make_leaf(
    issuer: x509.Certificate,
    issuer_key: ec.EllipticCurvePrivateKey | rsa.RSAPrivateKey,
    key: ec.EllipticCurvePrivateKey,
    digest: hashes.HashAlgorithm,
    with_authority_key_identifier: bool,
) -> x509.Certificate:
    not_before, not_after = validity()
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(issuer.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
    )
    if with_authority_key_identifier:
        builder = builder.add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()), critical=False
        )
    return builder.sign(issuer_key, digest)


class TlsServer:
    def __init__(self, workdir: Path, name: str, leaf: x509.Certificate, key: ec.EllipticCurvePrivateKey) -> None:
        chain = workdir / f"{name}-chain.pem"
        private = workdir / f"{name}-key.pem"
        chain.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
        private.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
        )
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(chain, private)
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.listener.settimeout(0.2)
        self.port = self.listener.getsockname()[1]
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self) -> None:
        while not self.stopping.is_set():
            try:
                connection, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            connection.settimeout(5)
            try:
                with self.context.wrap_socket(connection, server_side=True) as tls:
                    tls.sendall(b"ok")
            except ssl.SSLError, OSError:
                connection.close()

    def close(self) -> None:
        self.stopping.set()
        self.thread.join(timeout=5)
        self.listener.close()


def handshake(port: int) -> tuple[str, int | None, str]:
    context = OUTBOUND_CONTEXT["factory"]()
    try:
        with (
            socket.create_connection(("127.0.0.1", port), timeout=10) as raw,
            context.wrap_socket(raw, server_hostname="localhost") as tls,
        ):
            payload = tls.recv(2)
        return "accepted", None, payload.decode("ascii", "replace")
    except ssl.SSLCertVerificationError as error:
        return "rejected", error.verify_code, error.verify_message
    except (ssl.SSLError, OSError) as error:
        return "error", None, repr(error)


def update_ca_certificates() -> None:
    result = subprocess.run(["update-ca-certificates", "--fresh"], capture_output=True, text=True, check=False)
    print(f"update-ca-certificates --fresh exit {result.returncode}: {' '.join(result.stdout.split())}")
    if result.returncode != 0:
        raise RuntimeError(f"update-ca-certificates failed: {result.stderr.strip()}")


def deselect_like_the_dockerfile() -> None:
    subprocess.run(["sed", "-i", "-E", DOCKERFILE_DESELECT_EXPRESSION, str(CA_CONFIGURATION)], check=True)


def system_holds(certificate: x509.Certificate) -> dict[str, bool]:
    der = certificate.public_bytes(serialization.Encoding.DER)
    in_bundle = any(
        candidate.public_bytes(serialization.Encoding.DER) == der
        for candidate in x509.load_pem_x509_certificates(SYSTEM_BUNDLE.read_bytes())
    )
    in_directory = False
    for entry in SYSTEM_DIRECTORY.iterdir():
        if entry.name == SYSTEM_BUNDLE.name or not entry.resolve().is_file():
            continue
        data = entry.resolve().read_bytes()
        if b"-----BEGIN" not in data:
            continue
        if any(
            candidate.public_bytes(serialization.Encoding.DER) == der
            for candidate in x509.load_pem_x509_certificates(data)
        ):
            in_directory = True
            break
    in_context = der in OUTBOUND_CONTEXT["factory"]().get_ca_certs(binary_form=True)
    return {"bundle": in_bundle, "hashed-directory": in_directory, "outbound-context": in_context}


def probe_deselection(outcome: Outcome, workdir: Path) -> None:
    root_key = ec.generate_private_key(ec.SECP256R1())
    tag = uuid.uuid4().hex[:12]
    root_subject = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "PasarGuard image check"),
            x509.NameAttribute(NameOID.COMMON_NAME, f"PG Distrust Probe Root {tag}"),
        ]
    )
    root = make_root(root_subject, root_key, hashes.SHA256())
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf = make_leaf(root, root_key, leaf_key, hashes.SHA256(), with_authority_key_identifier=True)

    file_name = f"emSign_PG_Distrust_Probe_{tag}.crt"
    entry = f"mozilla/{file_name}"
    installed = MOZILLA_DIRECTORY / file_name
    original_configuration = CA_CONFIGURATION.read_text(encoding="utf-8")
    original_bundle = SYSTEM_BUNDLE.read_bytes()
    server = TlsServer(workdir, "probe", leaf, leaf_key)
    try:
        installed.write_bytes(root.public_bytes(serialization.Encoding.PEM))
        separator = "" if original_configuration.endswith("\n") else "\n"
        CA_CONFIGURATION.write_text(f"{original_configuration}{separator}{entry}\n", encoding="utf-8")
        update_ca_certificates()

        held = system_holds(root)
        outcome.check(
            "control: probe root is in the system store",
            held["bundle"] and held["hashed-directory"],
            f"{held}",
        )
        state, code, detail = handshake(server.port)
        outcome.check(
            "control: create_outbound_ssl_context() accepts the probe chain",
            state == "accepted",
            f"{state} {code} {detail}",
        )

        deselect_like_the_dockerfile()
        lines = CA_CONFIGURATION.read_text(encoding="utf-8").splitlines()
        outcome.check("deselect: configuration line is prefixed with !", f"!{entry}" in lines, f"!{entry}")
        update_ca_certificates()

        held = system_holds(root)
        outcome.check("deselect: probe root left every system store", not any(held.values()), f"{held}")
        state, code, detail = handshake(server.port)
        outcome.check(
            "deselect: create_outbound_ssl_context() rejects the probe chain",
            state == "rejected" and code == UNABLE_TO_GET_LOCAL_ISSUER,
            f"{state} {code} {detail}",
        )
    finally:
        server.close()
        installed.unlink(missing_ok=True)
        CA_CONFIGURATION.write_text(original_configuration, encoding="utf-8")
        update_ca_certificates()
    outcome.check(
        "cleanup: system bundle restored byte for byte",
        SYSTEM_BUNDLE.read_bytes() == original_bundle,
        str(SYSTEM_BUNDLE),
    )


def probe_emsign_subject(outcome: Outcome, workdir: Path, common_name: str) -> None:
    if EMSIGN_SUBJECTS[common_name][2] == "rsa":
        root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        digest = hashes.SHA256()
    else:
        root_key = ec.generate_private_key(ec.SECP384R1())
        digest = hashes.SHA384()
    root = make_root(emsign_subject(common_name), root_key, digest)
    outcome.check(
        f"emSign: the store check flags the {common_name} subject", is_distrusted(root), root.subject.rfc4514_string()
    )
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf = make_leaf(root, root_key, leaf_key, digest, with_authority_key_identifier=False)
    server = TlsServer(workdir, "emsign", leaf, leaf_key)
    try:
        state, code, detail = handshake(server.port)
    finally:
        server.close()
    outcome.check(
        f"emSign: create_outbound_ssl_context() finds no issuer named {common_name}",
        state == "rejected" and code == UNABLE_TO_GET_LOCAL_ISSUER,
        f"{state} {code} {detail}",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--code-root", type=Path, default=Path("/code"))
    arguments = parser.parse_args()
    if os.environ.get(THROWAWAY_FLAG) != "1" or not any(marker.exists() for marker in CONTAINER_MARKERS):
        print(
            f"refusing to edit the trust store: set {THROWAWAY_FLAG}=1 and run inside a throwaway container "
            f"({' or '.join(str(marker) for marker in CONTAINER_MARKERS)} must exist)",
            file=sys.stderr,
        )
        return 2
    OUTBOUND_CONTEXT["factory"] = load_outbound_context_factory(arguments.code_root)
    print(
        f"python {sys.version.split()[0]}, {ssl.OPENSSL_VERSION}, default verify paths {ssl.get_default_verify_paths()}"
    )
    outcome = Outcome()
    with tempfile.TemporaryDirectory() as workdir:
        probe_deselection(outcome, Path(workdir))
        for common_name in EMSIGN_SUBJECTS:
            probe_emsign_subject(outcome, Path(workdir), common_name)
    print("RESULT: PASS" if not outcome.failures else f"RESULT: FAIL ({', '.join(outcome.failures)})")
    return 0 if not outcome.failures else 1


if __name__ == "__main__":
    sys.exit(main())
