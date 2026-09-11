import hashlib
import hmac
import json
from unittest.mock import AsyncMock

import pytest
from fastapi import status

from app.jobs.send_notifications import send_to_all_webhooks
from app.models.settings import Telegram
from tests.api import client


class _FakeResponse:
    status = 200

    async def text(self):
        return ""


def _fake_client():
    client = AsyncMock()
    client.post = AsyncMock(return_value=_FakeResponse())
    return client


class _Webhook:
    def __init__(self, url: str, secret: str | None):
        self.url = url
        self.secret = secret


@pytest.mark.asyncio
async def test_webhook_payload_is_signed_with_hmac():
    client = _fake_client()
    webhook = _Webhook("https://example.com/hook", "topsecret")
    payload = [{"action": "user_created", "username": "alice"}]

    assert await send_to_all_webhooks(client, payload, [webhook]) is True

    _, kwargs = client.post.call_args
    expected = hmac.new(b"topsecret", kwargs["data"], hashlib.sha256).hexdigest()
    assert kwargs["headers"]["x-webhook-signature"] == f"sha256={expected}"
    assert kwargs["headers"]["x-webhook-secret"] == "topsecret"
    assert json.loads(kwargs["data"].decode("utf-8")) == payload


@pytest.mark.asyncio
async def test_webhook_without_secret_sends_no_signature_headers():
    client = _fake_client()
    webhook = _Webhook("https://example.com/hook", None)

    assert await send_to_all_webhooks(client, [{"action": "user_created"}], [webhook]) is True

    _, kwargs = client.post.call_args
    assert "x-webhook-secret" not in kwargs["headers"]
    assert "x-webhook-signature" not in kwargs["headers"]


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
