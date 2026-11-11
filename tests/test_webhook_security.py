import contextlib
import hashlib
import hmac
import json

import pytest
from aiohttp import web
from fastapi import status

from app.jobs.send_notifications import _send_webhook
from app.models.settings import Telegram, WebhookInfo
from app.notification.webhook import Notification, UserDataResetByNext, UserDataUsageReset
from app.utils.http_client import create_outbound_http_session
from tests.api import client


@contextlib.asynccontextmanager
async def _receiver():
    received: list[tuple[dict[str, str], bytes]] = []

    async def hook(request):
        received.append(({key.lower(): value for key, value in request.headers.items()}, await request.read()))
        return web.Response(status=200)

    application = web.Application()
    application.router.add_post("/hook", hook)
    runner = web.AppRunner(application, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        yield f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/hook", received
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_webhook_payload_is_signed_with_hmac():
    payload = [{"action": "user_created", "username": "alice"}]
    async with _receiver() as (url, received), create_outbound_http_session() as session:
        assert await _send_webhook(session, WebhookInfo(url=url, secret="topsecret"), payload) is True

    headers, body = received[0]
    expected = hmac.new(b"topsecret", body, hashlib.sha256).hexdigest()
    assert headers["x-webhook-signature"] == f"sha256={expected}"
    assert headers["x-webhook-secret"] == "topsecret"
    assert json.loads(body.decode("utf-8")) == payload


@pytest.mark.asyncio
async def test_webhook_without_secret_sends_no_signature_headers():
    async with _receiver() as (url, received), create_outbound_http_session() as session:
        assert await _send_webhook(session, WebhookInfo(url=url, secret=""), [{"action": "user_created"}]) is True

    headers, _ = received[0]
    assert "x-webhook-secret" not in headers
    assert "x-webhook-signature" not in headers


def _enable_telegram(monkeypatch: pytest.MonkeyPatch, secret: str):
    async def fake_telegram_settings():
        return Telegram(
            enable=True,
            token="1234567890:" + "a" * 35,
            webhook_url="https://example.com/tghook",
            webhook_secret=secret,
        )

    monkeypatch.setattr("app.routers.system.telegram_settings", fake_telegram_settings)


def test_telegram_webhook_rejects_wrong_secret(monkeypatch):
    _enable_telegram(monkeypatch, "expected-secret")
    response = client.post("/api/tghook", headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"}, json={})
    assert response.status_code == status.HTTP_403_FORBIDDEN


def test_telegram_webhook_accepts_configured_secret(monkeypatch):
    _enable_telegram(monkeypatch, "expected-secret")
    response = client.post("/api/tghook", headers={"X-Telegram-Bot-Api-Secret-Token": "expected-secret"}, json={})
    assert response.status_code == status.HTTP_200_OK


def test_data_reset_by_next_webhook_uses_its_own_action():
    assert UserDataResetByNext.model_fields["action"].default == Notification.Type.data_reset_by_next
    assert UserDataUsageReset.model_fields["action"].default == Notification.Type.data_usage_reset
