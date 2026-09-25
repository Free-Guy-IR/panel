from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import jwt
import pytest

from app.utils import jwt as jwt_utils

SECRET = "test-secret"


@pytest.fixture(autouse=True)
def fixed_secret(monkeypatch):
    monkeypatch.setattr(jwt_utils, "get_secret_key", AsyncMock(return_value=SECRET))


def _token(**claims):
    return jwt.encode({"sub": "admin", "access": "admin", **claims}, SECRET, algorithm="HS256")


@pytest.mark.parametrize("minutes", [0, 60])
@pytest.mark.asyncio
async def test_a_token_the_panel_issues_is_accepted(monkeypatch, minutes):
    monkeypatch.setattr(jwt_utils.jwt_settings, "access_token_expire_minutes", minutes)
    token = await jwt_utils.create_admin_token(7, "admin")
    payload = await jwt_utils.get_admin_payload(token)
    assert payload is not None
    assert payload["admin_id"] == 7
    assert payload["created_at"] is not None
    assert ("exp" in jwt.decode(token, SECRET, algorithms=["HS256"])) is (minutes > 0)


@pytest.mark.asyncio
async def test_a_token_without_exp_is_rejected_while_tokens_expire(monkeypatch):
    monkeypatch.setattr(jwt_utils.jwt_settings, "access_token_expire_minutes", 60)
    assert await jwt_utils.get_admin_payload(_token(iat=datetime.now(UTC))) is None


@pytest.mark.asyncio
async def test_a_token_without_exp_is_accepted_when_tokens_never_expire(monkeypatch):
    monkeypatch.setattr(jwt_utils.jwt_settings, "access_token_expire_minutes", 0)
    assert await jwt_utils.get_admin_payload(_token(iat=datetime.now(UTC) - timedelta(days=400))) is not None


@pytest.mark.parametrize("minutes", [0, 60])
@pytest.mark.asyncio
async def test_a_token_without_iat_is_rejected(monkeypatch, minutes):
    monkeypatch.setattr(jwt_utils.jwt_settings, "access_token_expire_minutes", minutes)
    claims = {"exp": datetime.now(UTC) + timedelta(minutes=30)} if minutes else {}
    assert await jwt_utils.get_admin_payload(_token(**claims)) is None


@pytest.mark.asyncio
async def test_a_token_with_an_unreadable_iat_is_rejected(monkeypatch):
    monkeypatch.setattr(jwt_utils.jwt_settings, "access_token_expire_minutes", 0)
    assert await jwt_utils.get_admin_payload(_token(iat="yesterday")) is None


@pytest.mark.asyncio
async def test_an_expired_token_is_still_rejected(monkeypatch):
    monkeypatch.setattr(jwt_utils.jwt_settings, "access_token_expire_minutes", 60)
    token = _token(iat=datetime.now(UTC) - timedelta(hours=2), exp=datetime.now(UTC) - timedelta(hours=1))
    assert await jwt_utils.get_admin_payload(token) is None


@pytest.mark.asyncio
async def test_an_out_of_range_iat_is_rejected_instead_of_crashing(monkeypatch):
    monkeypatch.setattr(jwt_utils.jwt_settings, "access_token_expire_minutes", 0)
    assert await jwt_utils.get_admin_payload(_token(iat=-(10**15))) is None
