import contextlib
import datetime
import ssl

import aiohttp
import pytest
from aiohttp import web
from aiohttp_socks import ProxyConnector
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app.utils import http_client
from tests.test_outbound_socks_proxy import TARGET_NAME, SocksProxy, _listening


def _issue(tmp_path):
    now = datetime.datetime.now(datetime.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "lane test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(1)
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
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
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, TARGET_NAME)]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(2)
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(TARGET_NAME)]), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    ca_file = tmp_path / "ca.pem"
    ca_file.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    chain_file = tmp_path / "server.pem"
    chain_file.write_bytes(
        certificate.public_bytes(serialization.Encoding.PEM)
        + key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return ca_file, chain_file


@contextlib.asynccontextmanager
async def _tls_target_behind_socks(chain_file):
    server_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    server_context.load_cert_chain(str(chain_file))

    async def hook(request):
        return web.Response(text="ok")

    application = web.Application()
    application.router.add_get("/hook", hook)
    runner = web.AppRunner(application, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_context)
    await site.start()
    target_port = site._server.sockets[0].getsockname()[1]
    proxy = SocksProxy(target_port, require_auth=False)
    try:
        async with _listening(proxy.handle) as proxy_port:
            yield proxy, proxy_port, f"https://{TARGET_NAME}:{target_port}/hook"
    finally:
        await runner.cleanup()


async def test_a_socks_proxied_https_request_verifies_with_the_outbound_ssl_context(tmp_path, monkeypatch):
    ca_file, chain_file = _issue(tmp_path)
    trusted = ssl.create_default_context(cafile=str(ca_file))
    monkeypatch.setattr(http_client, "create_outbound_ssl_context", lambda: trusted)

    async with (
        _tls_target_behind_socks(chain_file) as (proxy, proxy_port, url),
        http_client.create_outbound_http_session(
            proxy=f"socks5://127.0.0.1:{proxy_port}", timeout_seconds=5
        ) as session,
        session.get(url) as response,
    ):
        assert (response.status, await response.text()) == (200, "ok")

    assert proxy.destinations == [(TARGET_NAME, int(url.rsplit(":", 1)[1].split("/")[0]))]


async def test_the_same_request_without_the_outbound_context_fails_certificate_verification(tmp_path):
    _, chain_file = _issue(tmp_path)

    async with _tls_target_behind_socks(chain_file) as (_, proxy_port, url):
        connector = ProxyConnector.from_url(f"socks5://127.0.0.1:{proxy_port}", rdns=True)
        async with aiohttp.ClientSession(connector=connector) as session:
            with pytest.raises(aiohttp.ClientError) as refused:
                await session.get(url)

    assert "CERTIFICATE_VERIFY_FAILED" in str(refused.value)
