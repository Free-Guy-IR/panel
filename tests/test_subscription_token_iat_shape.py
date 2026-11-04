import asyncio
from datetime import UTC, datetime, timedelta

import jwt as pyjwt
import pytest
from fastapi import HTTPException

import app.operation as operation_module
from app.operation import BaseOperation, OperatorType
from app.utils import jwt as jwt_utils

SECRET = "a-secret-that-only-the-server-knows"
MISSING = object()


class _User:
    def __init__(self, user_id=42):
        self.id = user_id
        self.username = f"user{user_id}"
        self.created_at = datetime.now(UTC) - timedelta(days=1)
        self.sub_revoked_at = None


@pytest.fixture
def secret(monkeypatch):
    async def _secret():
        return SECRET

    monkeypatch.setattr(jwt_utils, "get_secret_key", _secret)


@pytest.fixture
def store(monkeypatch):
    users = {42: _User()}

    async def _get_user_by_id(_db, user_id, **_load_kwargs):
        return users.get(user_id)

    async def _get_user(_db, username, **_load_kwargs):
        return next((u for u in users.values() if u.username == username), None)

    monkeypatch.setattr(operation_module, "get_user_by_id", _get_user_by_id)
    monkeypatch.setattr(operation_module, "get_user", _get_user)
    return users


def _jwt(iat):
    claims = {"sub": "user42", "access": "subscription"}
    if iat is not MISSING:
        claims["iat"] = iat
    return pyjwt.encode(claims, SECRET, algorithm="HS256")


MALFORMED_IAT = [
    pytest.param(MISSING, id="missing"),
    pytest.param(None, id="null"),
    pytest.param([1700000000], id="list"),
    pytest.param({"t": 1700000000}, id="object"),
    pytest.param("1700000000", id="numeric-string"),
    pytest.param("not-a-number", id="text"),
]


@pytest.mark.parametrize("iat", MALFORMED_IAT)
def test_a_signed_subscription_jwt_with_a_malformed_iat_decodes_to_none(secret, iat):
    assert asyncio.run(jwt_utils.get_subscription_payload(_jwt(iat))) is None


@pytest.mark.parametrize("iat", MALFORMED_IAT)
def test_a_signed_subscription_jwt_with_a_malformed_iat_is_a_404_not_a_500(secret, store, iat):
    with pytest.raises(HTTPException) as refused:
        asyncio.run(BaseOperation(OperatorType.API).get_validated_sub(None, _jwt(iat)))
    assert refused.value.status_code == 404


def test_a_signed_subscription_jwt_with_a_numeric_iat_still_resolves(secret, store):
    token = _jwt(int(datetime.now(UTC).timestamp()))
    assert asyncio.run(BaseOperation(OperatorType.API).get_validated_sub(None, token)).id == 42


def test_a_float_iat_still_resolves(secret, store):
    token = _jwt(datetime.now(UTC).timestamp())
    assert asyncio.run(BaseOperation(OperatorType.API).get_validated_sub(None, token)).id == 42
