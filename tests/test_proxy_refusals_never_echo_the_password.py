import asyncio
import contextlib
import datetime
import logging
import socket
import uuid

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from PasarGuardNodeBridge import NodeType, create_node

from app.jobs import send_notifications as webhook_job
from app.models.settings import NotificationSettings, Webhook
from app.notification import client as notification_client, queue_manager
from app.notification.nats_queue import InMemoryNotificationQueue

SECRET = "Leak" + "Marker" + "77"
USER = "probe" + "user"


def _server_ca() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "node.example.test")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM).decode()


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _http_answer(status: bytes):
    async def answer(reader, writer):
        with contextlib.suppress(asyncio.IncompleteReadError, ConnectionError):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
        writer.close()

    return answer


async def _close_at_once(reader, writer):
    writer.close()


async def _socks5_refuses_the_login(reader, writer):
    with contextlib.suppress(asyncio.IncompleteReadError, ConnectionError):
        head = await reader.readexactly(2)
        await reader.readexactly(head[1])
        writer.write(b"\x05\x02")
        await writer.drain()
        await reader.readexactly(1)
        await reader.readexactly((await reader.readexactly(1))[0])
        await reader.readexactly((await reader.readexactly(1))[0])
        writer.write(b"\x01\x01")
        await writer.drain()
    writer.close()


REFUSALS = [
    pytest.param("http", _http_answer(b"407 Proxy Authentication Required"), id="http-407-on-connect"),
    pytest.param("http", _http_answer(b"502 Bad Gateway"), id="http-502-on-connect"),
    pytest.param("http", None, id="http-port-closed"),
    pytest.param("http", _close_at_once, id="http-closes-at-once"),
    pytest.param("socks5", _socks5_refuses_the_login, id="socks5-refuses-the-login"),
    pytest.param("socks5", None, id="socks5-port-closed"),
    pytest.param("socks5", _close_at_once, id="socks5-closes-at-once"),
]


@contextlib.asynccontextmanager
async def _proxy(scheme: str, handler):
    if handler is None:
        yield f"{scheme}://{USER}:{SECRET}@127.0.0.1:{_closed_port()}"
        return
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    try:
        yield f"{scheme}://{USER}:{SECRET}@127.0.0.1:{server.sockets[0].getsockname()[1]}"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize("transport", [NodeType.rest, NodeType.grpc], ids=["rest", "grpc"])
@pytest.mark.parametrize(("scheme", "handler"), REFUSALS)
async def test_a_node_error_behind_a_refusing_proxy_never_carries_the_proxy_password(transport, scheme, handler):
    async with _proxy(scheme, handler) as proxy:
        node = create_node(
            connection=transport,
            address="node.example.test",
            port=62050,
            api_port=62051,
            server_ca=_server_ca(),
            api_key=str(uuid.uuid4()),
            proxy=proxy,
            default_timeout=3,
            internal_timeout=3,
        )
        with pytest.raises(Exception) as refused:
            await asyncio.wait_for(node.info(timeout=3), 15)
        with contextlib.suppress(Exception):
            await node.stop()

    texts = [str(refused.value), repr(refused.value), str(getattr(refused.value, "detail", ""))]
    assert all(SECRET not in text for text in texts), texts


@pytest.mark.parametrize("status", [b"407 Proxy Authentication Required", b"502 Bad Gateway"], ids=["407", "502"])
async def test_a_notification_behind_a_refusing_http_proxy_logs_no_password(monkeypatch, caplog, status):
    async with _proxy("http", _http_answer(status)) as proxy:
        stored = NotificationSettings.model_validate({"max_retries": 2, "proxy_url": proxy})

        async def settings():
            return stored

        monkeypatch.setattr(notification_client, "notification_settings", settings)
        monkeypatch.setattr(notification_client, "client", None)
        caplog.set_level(logging.DEBUG)
        await notification_client.define_client()
        try:
            delivered = await notification_client._send_discord_webhook_direct(
                {"content": "probe"}, "https://hooks.example.test/x", 2
            )
        finally:
            await notification_client.client.close()

    assert delivered is False
    assert caplog.records
    assert SECRET not in caplog.text


@pytest.mark.parametrize("status", [b"407 Proxy Authentication Required", b"502 Bad Gateway"], ids=["407", "502"])
async def test_a_webhook_behind_a_refusing_http_proxy_logs_no_password(monkeypatch, caplog, status):
    monkeypatch.setattr(queue_manager, "webhook_queue_instance", InMemoryNotificationQueue())
    async with _proxy("http", _http_answer(status)) as proxy:
        stored = Webhook(
            enable=True,
            webhooks=[{"url": "https://hooks.example.test/x", "secret": "probe-secret"}],
            timeout=1,
            recurrent=1,
            proxy_url=proxy,
        )

        async def settings():
            return stored

        monkeypatch.setattr(webhook_job, "webhook_settings", settings)
        caplog.set_level(logging.DEBUG)
        await queue_manager.enqueue_webhook({"event": "probe"})
        await asyncio.wait_for(webhook_job.send_notifications(), timeout=15)

    assert any("Webhook delivery failed" in record.getMessage() for record in caplog.records)
    assert SECRET not in caplog.text
