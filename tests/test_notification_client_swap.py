import asyncio
import contextlib
import random

import pytest

from app.models.settings import NotificationSettings
from app.notification import client as notification_client

HOOK = "http://hooks.example.test/notify"


@pytest.fixture(autouse=True)
def no_installed_client(monkeypatch):
    monkeypatch.setattr(notification_client, "client", None)


def _stored(proxy_url: str | None) -> NotificationSettings:
    return NotificationSettings.model_validate({"max_retries": 2, "proxy_url": proxy_url})


@contextlib.asynccontextmanager
async def _http_proxy():
    seen: list[bytes] = []

    async def handle(reader, writer):
        with contextlib.suppress(asyncio.IncompleteReadError, ConnectionError):
            seen.append((await reader.readuntil(b"\r\n\r\n")).split(b"\r\n")[0])
            writer.write(b"HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n")
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}", seen
    finally:
        server.close()
        await server.wait_closed()


async def _let_pending_tasks_run() -> None:
    for _ in range(5):
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)


async def _send() -> bool:
    return await notification_client._send_discord_webhook_direct({"content": "probe"}, HOOK, 2)


async def test_the_latest_of_two_overlapping_proxy_changes_stays_installed(monkeypatch):
    async with (
        _http_proxy() as (first_proxy, _),
        _http_proxy() as (older_proxy, older_seen),
        _http_proxy() as (newer_proxy, newer_seen),
    ):
        older_read = asyncio.Event()
        stored = {"reads": 0}

        async def stored_settings():
            stored["reads"] += 1
            if stored["reads"] == 1:
                return _stored(first_proxy)
            if stored["reads"] == 2:
                await older_read.wait()
                return _stored(older_proxy)
            return _stored(newer_proxy)

        monkeypatch.setattr(notification_client, "notification_settings", stored_settings)
        await notification_client.define_client()
        first = notification_client.client
        older = asyncio.create_task(notification_client.define_client())
        await _let_pending_tasks_run()
        await notification_client.define_client()
        newer = notification_client.client
        older_read.set()
        await older
        final = notification_client.client
        await _let_pending_tasks_run()
        try:
            delivered = await _send()
            left_open = [session for session in (first, newer) if session is not final and not session.closed]
        finally:
            for session in {first, newer, final}:
                await session.close()

    assert final is newer
    assert left_open == []
    assert delivered is True
    assert newer_seen == [f"POST {HOOK} HTTP/1.1".encode()]
    assert older_seen == []


async def test_a_notification_sent_while_a_proxy_change_reads_the_settings_is_delivered(monkeypatch):
    async with _http_proxy() as (proxy, seen):
        read_started = asyncio.Event()
        release = asyncio.Event()
        stored = {"slow": False}

        async def stored_settings():
            if stored["slow"]:
                read_started.set()
                await release.wait()
            return _stored(proxy)

        monkeypatch.setattr(notification_client, "notification_settings", stored_settings)
        await notification_client.define_client()
        first = notification_client.client
        stored["slow"] = True
        change = asyncio.create_task(notification_client.define_client())
        await read_started.wait()
        await _let_pending_tasks_run()
        try:
            delivered = await _send()
        finally:
            release.set()
            await change
            await _let_pending_tasks_run()
            replaced_closed = first.closed or first is notification_client.client
            for session in {first, notification_client.client}:
                await session.close()

    assert delivered is True
    assert seen == [f"POST {HOOK} HTTP/1.1".encode()]
    assert replaced_closed


async def test_every_session_a_proxy_change_replaces_is_closed(monkeypatch):
    async def stored_settings():
        return _stored(None)

    monkeypatch.setattr(notification_client, "notification_settings", stored_settings)
    sessions = []
    for _ in range(3):
        await notification_client.define_client()
        sessions.append(notification_client.client)
    await _let_pending_tasks_run()
    closed = [session.closed for session in sessions]
    await sessions[-1].close()

    assert closed == [True, True, False]


@pytest.mark.parametrize("seed", range(5))
async def test_many_overlapping_proxy_changes_end_on_the_last_one_and_leak_no_session(monkeypatch, seed):
    delays = random.Random(seed)
    built: dict[int, str | None] = {}
    created = []
    real_factory = notification_client.create_outbound_http_session

    def recording_factory(**kwargs):
        session = real_factory(**kwargs)
        created.append(session)
        built[id(session)] = kwargs.get("proxy")
        return session

    calls = {"started": 0}

    async def stored_settings():
        calls["started"] += 1
        number = calls["started"]
        await asyncio.sleep(delays.random() / 50)
        return _stored(f"http://127.0.0.1:{20000 + number}")

    monkeypatch.setattr(notification_client, "create_outbound_http_session", recording_factory)
    monkeypatch.setattr(notification_client, "notification_settings", stored_settings)
    changes = []
    for _ in range(12):
        changes.append(asyncio.create_task(notification_client.define_client()))
        await asyncio.sleep(0)
    await asyncio.gather(*changes)
    await _let_pending_tasks_run()
    final = notification_client.client
    left_open = [session for session in created if session is not final and not session.closed]
    for session in created:
        await session.close()

    assert built[id(final)] == "http://127.0.0.1:20012"
    assert left_open == []
