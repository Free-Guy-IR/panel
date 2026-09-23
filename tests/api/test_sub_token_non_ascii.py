from base64 import b64encode

import pytest
from fastapi.testclient import TestClient

from app.models.settings import Telegram
from app.utils.jwt import get_subscription_payload
from tests.api import app


def _legacy_shaped_token() -> str:
    payload = b64encode(b"someone,1700000000", altchars=b"-_").decode().rstrip("=")
    return payload + "é" * 10


@pytest.mark.asyncio
async def test_legacy_token_with_non_ascii_signature_is_rejected_not_raised():
    assert await get_subscription_payload(_legacy_shaped_token()) is None


def test_subscription_route_answers_a_client_error_for_a_non_ascii_token():
    probe = TestClient(app, raise_server_exceptions=False)
    response = probe.get(f"/sub/{_legacy_shaped_token()}/info")
    assert response.status_code < 500, response.status_code


def test_telegram_webhook_answers_forbidden_for_a_non_ascii_secret(monkeypatch):
    async def enabled_telegram():
        return Telegram(
            enable=True,
            token="1234567890:" + "a" * 35,
            webhook_url="https://example.com/tghook",
            webhook_secret="expected-secret",
        )

    monkeypatch.setattr("app.routers.system.telegram_settings", enabled_telegram)
    probe = TestClient(app, raise_server_exceptions=False)
    response = probe.post("/api/tghook", headers={"X-Telegram-Bot-Api-Secret-Token": b"\xe9xpected-secret"}, json={})
    assert response.status_code == 403, response.status_code
