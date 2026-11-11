import asyncio
import contextlib
import socket

import pytest

from app.jobs import send_notifications as webhook_job
from app.models.settings import NotificationSettings, Webhook
from app.notification import client as notification_client, queue_manager
from app.notification.nats_queue import InMemoryNotificationQueue
from app.utils.http_client import create_outbound_http_session

USER = "probe" + "user"
PASSWORD = "probe" + "pass"
TARGET_NAME = "hooks.example.test"


class Target:
    def __init__(self):
        self.requests: list[str] = []

    async def handle(self, reader, writer):
        with contextlib.suppress(asyncio.IncompleteReadError, ConnectionError):
            head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1").split("\r\n")
            self.requests.append(head[0])
            length = next(
                (int(line.split(":", 1)[1]) for line in head if line.lower().startswith("content-length:")), 0
            )
            await reader.readexactly(length)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
        writer.close()


class SocksProxy:
    def __init__(self, target_port: int, *, require_auth: bool = True):
        self.target_port = target_port
        self.require_auth = require_auth
        self.first_bytes: list[bytes] = []
        self.destinations: list[tuple[str, int]] = []
        self.logins: list[tuple[bytes, bytes]] = []

    async def handle(self, reader, writer):
        with contextlib.suppress(asyncio.IncompleteReadError, ConnectionError):
            first = await reader.read(1)
            if first == b"\x05":
                self.first_bytes.append(first)
                destination = await self._socks5(reader, writer)
            elif first == b"\x04":
                self.first_bytes.append(first)
                destination = await self._socks4(reader, writer)
            else:
                self.first_bytes.append(first + await reader.read(4096))
                destination = None
            if destination is not None:
                self.destinations.append(destination)
                await self._relay(reader, writer)
        writer.close()

    async def _socks5(self, reader, writer):
        methods = await reader.readexactly((await reader.readexactly(1))[0])
        method = 2 if self.require_auth else 0
        if method not in methods:
            writer.write(b"\x05\xff")
            return None
        writer.write(bytes([5, method]))
        if method == 2:
            await reader.readexactly(1)
            user = await reader.readexactly((await reader.readexactly(1))[0])
            password = await reader.readexactly((await reader.readexactly(1))[0])
            self.logins.append((user, password))
            if (user, password) != (USER.encode(), PASSWORD.encode()):
                writer.write(b"\x01\x01")
                return None
            writer.write(b"\x01\x00")
        _, _, _, address_type = await reader.readexactly(4)
        if address_type == 3:
            host = (await reader.readexactly((await reader.readexactly(1))[0])).decode()
        elif address_type == 1:
            host = socket.inet_ntoa(await reader.readexactly(4))
        else:
            return None
        port = int.from_bytes(await reader.readexactly(2), "big")
        writer.write(b"\x05\x00\x00\x01" + socket.inet_aton("127.0.0.1") + port.to_bytes(2, "big"))
        return host, port

    async def _socks4(self, reader, writer):
        header = await reader.readexactly(7)
        port = int.from_bytes(header[1:3], "big")
        user = (await reader.readuntil(b"\x00"))[:-1]
        self.logins.append((user, b""))
        host = socket.inet_ntoa(header[3:7])
        if header[3:6] == b"\x00\x00\x00" and header[6] != 0:
            host = (await reader.readuntil(b"\x00"))[:-1].decode()
        writer.write(b"\x00\x5a" + header[1:7])
        return host, port

    async def _relay(self, reader, writer):
        target_reader, target_writer = await asyncio.open_connection("127.0.0.1", self.target_port)

        async def pipe(source, sink):
            with contextlib.suppress(ConnectionError):
                while data := await source.read(65536):
                    sink.write(data)
                    await sink.drain()
            sink.close()

        await asyncio.gather(pipe(reader, target_writer), pipe(target_reader, writer))


@contextlib.asynccontextmanager
async def _listening(handler):
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


@contextlib.asynccontextmanager
async def _socks_route(require_auth: bool = True):
    target = Target()
    async with _listening(target.handle) as target_port:
        proxy = SocksProxy(target_port, require_auth=require_auth)
        async with _listening(proxy.handle) as proxy_port:
            yield target, proxy, proxy_port, f"http://{TARGET_NAME}:{target_port}/hook"


def _assert_spoken_in_socks(proxy: SocksProxy, version: bytes) -> None:
    assert [first[:1] for first in proxy.first_bytes] == [version]
    assert not any(b"Proxy-Authorization" in first for first in proxy.first_bytes)


@pytest.mark.parametrize(
    ("scheme", "credentials", "version", "login"),
    [
        ("socks5", f"{USER}:{PASSWORD}@", b"\x05", [(USER.encode(), PASSWORD.encode())]),
        ("socks5", "", b"\x05", []),
        ("socks4", f"{USER}:{PASSWORD}@", b"\x04", [(USER.encode(), b"")]),
    ],
    ids=["socks5-login", "socks5-open", "socks4a"],
)
async def test_an_outbound_request_through_a_socks_proxy_reaches_its_target(scheme, credentials, version, login):
    async with (
        _socks_route(require_auth=bool(credentials) and scheme == "socks5") as (target, proxy, port, url),
        create_outbound_http_session(proxy=f"{scheme}://{credentials}127.0.0.1:{port}", timeout_seconds=5) as session,
        session.get(url) as response,
    ):
        status, body = response.status, await response.text()

    assert (status, body) == (200, "ok")
    assert target.requests == ["GET /hook HTTP/1.1"]
    assert proxy.destinations == [(TARGET_NAME, int(url.rsplit(":", 1)[1].split("/")[0]))]
    assert proxy.logins == login
    _assert_spoken_in_socks(proxy, version)


async def test_an_http_proxy_still_gets_an_http_proxy_request():
    received: list[bytes] = []

    async def http_proxy(reader, writer):
        received.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await writer.drain()
        writer.close()

    async with (
        _listening(http_proxy) as port,
        create_outbound_http_session(proxy=f"http://{USER}:{PASSWORD}@127.0.0.1:{port}", timeout_seconds=5) as session,
        session.get(f"http://{TARGET_NAME}/hook") as response,
    ):
        assert response.status == 200

    lines = received[0].split(b"\r\n")
    assert lines[0] == f"GET http://{TARGET_NAME}/hook HTTP/1.1".encode()
    assert any(line.lower().startswith(b"proxy-authorization: basic ") for line in lines)


async def test_a_notification_through_a_socks_proxy_is_delivered(monkeypatch):
    async with _socks_route() as (target, proxy, port, url):
        stored = NotificationSettings.model_validate(
            {"max_retries": 2, "proxy_url": f"socks5://{USER}:{PASSWORD}@127.0.0.1:{port}"}
        )

        async def settings():
            return stored

        monkeypatch.setattr(notification_client, "notification_settings", settings)
        monkeypatch.setattr(notification_client, "client", None)
        await notification_client.define_client()
        try:
            delivered = await notification_client._send_discord_webhook_direct({"content": "probe"}, url, 2)
        finally:
            await notification_client.client.close()

    assert delivered is True
    assert target.requests == ["POST /hook HTTP/1.1"]
    _assert_spoken_in_socks(proxy, b"\x05")


async def test_a_webhook_through_a_socks_proxy_is_delivered(monkeypatch):
    monkeypatch.setattr(queue_manager, "webhook_queue_instance", InMemoryNotificationQueue())
    async with _socks_route() as (target, proxy, port, url):
        stored = Webhook(
            enable=True,
            webhooks=[{"url": url, "secret": "probe-secret"}],
            timeout=1,
            recurrent=1,
            proxy_url=f"socks5://{USER}:{PASSWORD}@127.0.0.1:{port}",
        )

        async def settings():
            return stored

        monkeypatch.setattr(webhook_job, "webhook_settings", settings)
        await queue_manager.enqueue_webhook({"event": "probe"})
        await asyncio.wait_for(webhook_job.send_notifications(), timeout=15)

    assert target.requests == ["POST /hook HTTP/1.1"]
    _assert_spoken_in_socks(proxy, b"\x05")
