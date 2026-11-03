import asyncio
import contextlib
import logging
from types import SimpleNamespace

import aiohttp
import pytest

from app.jobs import send_notifications as send_module
from app.notification import client as client_module, queue_manager
from app.notification.nats_queue import InMemoryNotificationQueue

URL_TOKEN = "Tk7UrlSecret"
BODY_TOKEN = "Bd8BodySecret"
WEBHOOK_URL = f"https://hook:{URL_TOKEN}@hooks.example.com/notify?token={URL_TOKEN}"
DISCORD_URL = f"https://discord.com/api/webhooks/1234/{URL_TOKEN}"


class _Response:
    def __init__(self, status: int):
        self.status = status
        self.released = False

    def release(self):
        self.released = True

    async def text(self):
        return f'{{"error": "denied", "echo": "{BODY_TOKEN}"}}'

    async def json(self):
        return {"echo": BODY_TOKEN}


class _Client:
    def __init__(self, *, status: int | None = None, error: Exception | None = None):
        self.status = status
        self.error = error
        self.responses: list[_Response] = []

    async def post(self, url, **kwargs):
        if self.error is not None:
            raise self.error
        self.responses.append(_Response(self.status))
        return self.responses[-1]


def _assert_nothing_secret(caplog):
    logged = caplog.text
    assert URL_TOKEN not in logged
    assert BODY_TOKEN not in logged
    assert "hooks.example.com" not in logged
    assert "discord.com/api/webhooks" not in logged


@pytest.mark.asyncio
async def test_a_failed_webhook_never_logs_its_url_or_the_response_body(caplog):
    webhook = SimpleNamespace(url=WEBHOOK_URL, secret=None)
    failing = _Client(status=500)
    with caplog.at_level(logging.DEBUG):
        assert await send_module.send_to_all_webhooks(failing, [{"event": "x"}], [webhook]) is False
    _assert_nothing_secret(caplog)
    assert "Webhook delivery failed with HTTP 500" in caplog.text
    assert [response.released for response in failing.responses] == [True]


@pytest.mark.asyncio
async def test_a_webhook_connection_error_never_logs_the_url(caplog):
    webhook = SimpleNamespace(url=WEBHOOK_URL, secret=None)
    error = aiohttp.ClientConnectionError(f"Cannot connect through {WEBHOOK_URL}")
    with caplog.at_level(logging.DEBUG):
        assert await send_module.send_to_all_webhooks(_Client(error=error), [{"event": "x"}], [webhook]) is False
    _assert_nothing_secret(caplog)
    assert "Webhook delivery failed due to a connection error or timeout" in caplog.text


@pytest.mark.asyncio
async def test_a_discord_or_telegram_failure_never_logs_the_body_the_url_or_the_bot_token(caplog, monkeypatch):
    rejecting = _Client(status=400)
    with caplog.at_level(logging.DEBUG):
        monkeypatch.setattr(client_module, "client", rejecting)
        assert await client_module._send_discord_webhook_direct({"content": "x"}, DISCORD_URL, 1) is False
        assert await client_module._send_telegram_message_direct("x", 1, None, 1, f"123:{URL_TOKEN}") is False
        failure = aiohttp.InvalidURL(f"https://api.telegram.org/bot123:{URL_TOKEN}/sendMessage")
        monkeypatch.setattr(client_module, "client", _Client(error=failure))
        assert await client_module._send_discord_webhook_direct({"content": "x"}, DISCORD_URL, 1) is False
        assert await client_module._send_telegram_message_direct("x", 1, None, 1, f"123:{URL_TOKEN}") is False
    _assert_nothing_secret(caplog)
    assert caplog.text.count("Notification delivery failed with HTTP 400") == 2
    assert caplog.text.count("Notification delivery failed due to a connection error or timeout") == 2
    assert [response.released for response in rejecting.responses] == [True, True]


@pytest.mark.asyncio
async def test_an_unknown_or_malformed_notification_is_discarded_without_logging_its_content(caplog):
    with caplog.at_level(logging.DEBUG):
        await client_module.process_notification({"type": "pager", "webhook": DISCORD_URL})
        await client_module.process_notification({"type": "discord", "webhook": DISCORD_URL, "json_data": "x"})
    _assert_nothing_secret(caplog)
    assert "Discarding unknown notification type" in caplog.text
    assert "Discarding malformed notification" in caplog.text


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
