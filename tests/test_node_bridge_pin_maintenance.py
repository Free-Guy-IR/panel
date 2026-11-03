import asyncio
import datetime
import ipaddress
import ssl
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from PasarGuardNodeBridge import controller as bridge_controller
from PasarGuardNodeBridge.storage import LifecycleOperation

from app.db.models import NodeConnectionType
from app.node import NodeManager
from app.node.bridge import create_node

NODE_SERVICED_DEADLINE = 300
SLOW_REPLY_SECONDS = 1.5


def _self_signed_for_loopback(directory):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_pem = certificate.public_bytes(serialization.Encoding.PEM)
    cert_path = directory / "node.crt"
    key_path = directory / "node.key"
    cert_path.write_bytes(cert_pem)
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
        )
    )
    return cert_pem.decode(), cert_path, key_path


class SlowNodeService:
    def __init__(self, api_key):
        self.api_key = api_key
        self.maintenance_calls = []

    async def root(self, request):
        return web.json_response({})

    async def maintenance(self, request):
        assert request.headers.get("x-api-key") == self.api_key
        self.maintenance_calls.append(request.path)
        await asyncio.sleep(SLOW_REPLY_SECONDS)
        return web.json_response({"detail": "done"})


@asynccontextmanager
async def slow_node_service(tmp_path):
    server_ca, cert_path, key_path = _self_signed_for_loopback(tmp_path)
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(cert_path, key_path)
    service = SlowNodeService(str(uuid4()))
    application = web.Application()
    application.router.add_get("/", service.root)
    for path in ("/node/update", "/node/core_update", "/node/geofiles"):
        application.router.add_post(path, service.maintenance)
    runner = web.AppRunner(application)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=context)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield service, server_ca, port
    finally:
        await runner.cleanup()


def _panel_node_row(connection_type, server_ca, api_key, port):
    return SimpleNamespace(
        id=4242,
        name="maintenance-node",
        connection_type=connection_type,
        address="127.0.0.1",
        port=port,
        api_port=port,
        server_ca=server_ca,
        api_key=api_key,
        default_timeout=1,
        internal_timeout=1,
        proxy_url=None,
        usage_coefficient=1.0,
    )


def test_the_pinned_bridge_waits_out_node_serviced_for_every_update_operation():
    timeouts = bridge_controller.MAINTENANCE_TIMEOUTS
    for operation in (
        LifecycleOperation.UPDATE_NODE,
        LifecycleOperation.UPDATE_CORE,
        LifecycleOperation.UPDATE_GEOFILES,
    ):
        assert timeouts[operation] >= NODE_SERVICED_DEADLINE


@pytest.mark.parametrize("connection_type", [NodeConnectionType.grpc, NodeConnectionType.rest])
async def test_a_maintenance_call_slower_than_the_node_default_timeout_still_succeeds(
    monkeypatch, tmp_path, connection_type
):
    monkeypatch.setattr("app.node.get_bridge_memory", lambda: (None, None, None))
    async with slow_node_service(tmp_path) as (service, server_ca, port):
        manager = NodeManager()
        row = _panel_node_row(connection_type, server_ca, service.api_key, port)
        node = create_node(**manager._create_node_kwargs(row))
        try:
            update = await node.update_node()
            core = await node.update_core({"core_version": "latest"})
            geofiles = await node.update_geofiles({"region": "iran"})
        finally:
            await node._json_client.close()
            client = getattr(node, "_client", None)
            if hasattr(client, "close"):
                result = client.close()
                if asyncio.iscoroutine(result):
                    await result
    for response in (update, core, geofiles):
        assert response.status_code == 200
    assert service.maintenance_calls == ["/node/update", "/node/core_update", "/node/geofiles"]
