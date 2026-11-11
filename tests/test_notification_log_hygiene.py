import asyncio
import contextlib
import logging
import socket
from types import SimpleNamespace

import pytest
from aiohttp import web

from app.jobs import send_notifications as send_module
from app.models.settings import NotificationSettings, WebhookInfo
from app.notification import client as client_module, queue_manager
from app.notification.nats_queue import InMemoryNotificationQueue
from app.utils.http_client import create_outbound_http_session

URL_TOKEN = "Tk7UrlSecret"
BODY_TOKEN = "Bd8BodySecret"
WEBHOOK_URL = f"https://hook:{URL_TOKEN}@hooks.example.com/notify?token={URL_TOKEN}"
DISCORD_URL = f"https://discord.com/api/webhooks/1234/{URL_TOKEN}"


@contextlib.asynccontextmanager
async def _rejecting_receiver(status: int):
    seen: list[str] = []

    async def reject(request):
        seen.append(request.path)
        return web.json_response({"error": "denied", "echo": BODY_TOKEN}, status=status)

    application = web.Application()
    application.router.add_post("/{tail:.*}", reject)
    runner = web.AppRunner(application, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        yield f"127.0.0.1:{site._server.sockets[0].getsockname()[1]}", seen
    finally:
        await runner.cleanup()


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _assert_nothing_secret(caplog):
    logged = caplog.text
    assert URL_TOKEN not in logged
    assert BODY_TOKEN not in logged
    assert "hooks.example.com" not in logged
    assert "discord.com/api/webhooks" not in logged


async def _use_notifications(monkeypatch: pytest.MonkeyPatch) -> InMemoryNotificationQueue:
    queue = InMemoryNotificationQueue()
    monkeypatch.setattr(queue_manager, "queue_instance", queue)
    stored = NotificationSettings.model_validate(
        {"notify_discord": True, "discord_webhook_url": DISCORD_URL, "max_retries": 2}
    )

    async def settings():
        return stored

    monkeypatch.setattr(client_module, "notification_settings", settings)
    monkeypatch.setattr(client_module, "client", None)
    await client_module.define_client()
    return queue


@pytest.mark.asyncio
async def test_a_failed_webhook_never_logs_its_url_or_the_response_body(caplog):
    async with _rejecting_receiver(500) as (address, seen), create_outbound_http_session() as session:
        webhook = WebhookInfo(url=f"http://hook:{URL_TOKEN}@{address}/notify?token={URL_TOKEN}", secret="")
        with caplog.at_level(logging.DEBUG):
            assert await send_module._send_webhook(session, webhook, [{"event": "x"}]) is False
    assert seen == ["/notify"]
    _assert_nothing_secret(caplog)
    assert "Webhook delivery failed with HTTP 500" in caplog.text


@pytest.mark.asyncio
async def test_a_webhook_connection_error_never_logs_the_url(caplog):
    webhook = WebhookInfo(url=f"http://hook:{URL_TOKEN}@127.0.0.1:{_closed_port()}/notify?token={URL_TOKEN}", secret="")
    async with create_outbound_http_session() as session:
        with caplog.at_level(logging.DEBUG):
            assert await send_module._send_webhook(session, webhook, [{"event": "x"}]) is False
    _assert_nothing_secret(caplog)
    assert "Webhook delivery failed due to a connection error or timeout" in caplog.text


@pytest.mark.asyncio
async def test_a_discord_or_telegram_failure_never_logs_the_body_the_url_or_the_bot_token(caplog, monkeypatch):
    queue = await _use_notifications(monkeypatch)
    async with _rejecting_receiver(400) as (address, seen):
        with caplog.at_level(logging.DEBUG):
            await queue_manager.enqueue_discord({"content": "x"}, f"http://{address}/api/webhooks/1234/{URL_TOKEN}")
            await client_module.process_notification(await queue.dequeue(timeout=0))
            assert await client_module._post_notification(
                f"http://{address}/bot123:{URL_TOKEN}/sendMessage", data={"text": "x"}
            ) == (False, 1)
            await queue_manager.enqueue_discord({"content": "x"}, f"http://[{URL_TOKEN}/api/webhooks/1234")
            await client_module.process_notification(await queue.dequeue(timeout=0))
        await client_module.client.close()
    assert seen == [f"/api/webhooks/1234/{URL_TOKEN}", f"/bot123:{URL_TOKEN}/sendMessage"]
    _assert_nothing_secret(caplog)
    assert caplog.text.count("Notification delivery failed with HTTP 400") == 2
    assert caplog.text.count("Notification delivery failed due to a connection error or timeout") == 1


@pytest.mark.asyncio
async def test_an_unknown_or_malformed_notification_is_discarded_without_logging_its_content(caplog, monkeypatch):
    queue = await _use_notifications(monkeypatch)
    await queue.enqueue({"type": "pager", "webhook": DISCORD_URL})
    await queue.enqueue({"type": "discord", "webhook": DISCORD_URL, "json_data": "x"})
    with caplog.at_level(logging.DEBUG):
        await client_module.process_notification(await queue.dequeue(timeout=0))
        await client_module.process_notification(await queue.dequeue(timeout=0))
    await client_module.client.close()
    _assert_nothing_secret(caplog)
    assert "Discarding unknown notification type" in caplog.text
    assert "Discarding malformed notification" in caplog.text
    assert await queue.dequeue(timeout=0) is None


@pytest.mark.asyncio
async def test_an_unreadable_webhook_notification_is_discarded_without_logging_its_content(caplog, monkeypatch):
    webhooks = InMemoryNotificationQueue()
    monkeypatch.setattr(queue_manager, "webhook_queue_instance", webhooks)

    async def settings():
        return SimpleNamespace(enable=True, proxy_url=None, recurrent=3, timeout=0.3, webhooks=[])

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(send_module, "webhook_settings", settings)
    monkeypatch.setattr(send_module, "create_outbound_http_session", lambda proxy=None: _Session())
    await webhooks.enqueue({"payload": {"subscription_url": WEBHOOK_URL}, "tries": "many", "echo": BODY_TOKEN})

    with caplog.at_level(logging.DEBUG):
        task = asyncio.create_task(send_module.send_notifications())
        for _ in range(100):
            if "Discarding malformed webhook notification" in caplog.text:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    _assert_nothing_secret(caplog)
    assert "Discarding malformed webhook notification" in caplog.text
