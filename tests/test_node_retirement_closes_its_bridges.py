import asyncio
import contextlib
import datetime as dt
import gc
import ipaddress
import socket
import ssl
import warnings
from uuid import uuid4

import aiohttp
from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from PasarGuardNodeBridge.common import service_pb2 as service
from sqlalchemy import select

from app.db.models import Node, NodeConnectionType
from app.fork.models.node_retirement import NodeRetirement
from app.fork.operation import node_retirement
from app.node import nats_memory
from app.operation import node as node_operation
from tests.api.helpers import unique_name
from tests.test_node_delete_stop_contract import Session, delete, forget_every_owed_stop, quiet_side_effects

UNCLOSED = ("Unclosed client session", "Unclosed connector")


def _tls(tmp_path) -> tuple[str, ssl.SSLContext]:
    now = dt.datetime.now(dt.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "retirement test ca")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
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
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    cert_file, key_file = tmp_path / "node.pem", tmp_path / "node.key"
    cert_file.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(cert_file, key_file)
    return ca.public_bytes(serialization.Encoding.PEM).decode(), context


class RestNodeService:
    def __init__(self):
        self.started = True
        self.stops = 0

    async def info(self, request: web.Request) -> web.Response:
        reply = service.BaseInfoResponse(started=self.started, core_version="25.1.1", node_version="0.5.5")
        return web.Response(body=reply.SerializeToString(), content_type="application/x-protobuf")

    async def stop(self, request: web.Request) -> web.Response:
        self.stops += 1
        self.started = False
        return web.Response(body=service.Empty().SerializeToString(), content_type="application/x-protobuf")


@contextlib.asynccontextmanager
async def serving(rest_node: RestNodeService, context: ssl.SSLContext, port: int):
    app = web.Application()
    app.router.add_get("/info", rest_node.info)
    app.router.add_put("/stop", rest_node.stop)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port, ssl_context=context).start()
    try:
        yield
    finally:
        await runner.cleanup()


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def seed_rest_node(server_ca: str, port: int) -> Node:
    async with Session() as session:
        row = Node(
            name=unique_name("rest_retire"),
            address="127.0.0.1",
            port=port,
            api_port=port,
            server_ca=server_ca,
            api_key=str(uuid4()),
            core_config_id=None,
        )
        row.connection_type = NodeConnectionType.rest
        session.add(row)
        await session.commit()
        return row


async def stops_owed_to(port: int) -> int:
    async with Session() as session:
        found = await session.execute(
            select(NodeRetirement.id).where(NodeRetirement.address == "127.0.0.1", NodeRetirement.port == port)
        )
        return len(found.all())


async def delete_while_down_then_drain_once_up(monkeypatch, tmp_path) -> RestNodeService:
    quiet_side_effects(monkeypatch)
    for module in (nats_memory, node_operation, node_retirement):
        monkeypatch.setattr(module, "needs_shared_bridge_memory", lambda: False)
    await forget_every_owed_stop()
    server_ca, context = _tls(tmp_path)
    port = free_port()
    row = await seed_rest_node(server_ca, port)

    await delete(row, bulk=False)
    assert await stops_owed_to(port) == 1

    rest_node = RestNodeService()
    async with serving(rest_node, context, port):
        await node_retirement.drain_node_retirements(now=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=2))
    assert rest_node.stops == 1
    assert await stops_owed_to(port) == 0
    return rest_node


async def test_a_delete_and_an_owed_stop_drain_close_every_node_session_they_open(monkeypatch, tmp_path):
    opened: list[aiohttp.ClientSession] = []
    original_init = aiohttp.ClientSession.__init__

    def recording_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        opened.append(self)

    monkeypatch.setattr(aiohttp.ClientSession, "__init__", recording_init)

    await delete_while_down_then_drain_once_up(monkeypatch, tmp_path)

    left_open = [session for session in opened if not session.closed]
    for session in left_open:
        await session.close()
    assert opened
    assert not left_open, f"{len(left_open)} of {len(opened)} node sessions were left open"


async def test_a_delete_and_an_owed_stop_drain_leave_nothing_for_aiohttp_to_report(monkeypatch, tmp_path):
    reports: list[str] = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reports.append(str(context.get("message", ""))))
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            await delete_while_down_then_drain_once_up(monkeypatch, tmp_path)
            gc.collect()
            await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous)

    reported = [message for message in reports if message.startswith(UNCLOSED)]
    reported += [str(entry.message) for entry in caught if str(entry.message).startswith(UNCLOSED)]
    assert not reported, reported
