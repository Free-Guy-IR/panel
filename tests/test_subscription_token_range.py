import asyncio
import hmac
from base64 import b64encode
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import jwt as pyjwt
import pytest
from fastapi import HTTPException

import app.operation as operation_module
from app.operation import BaseOperation, OperatorType
from app.utils import jwt as jwt_utils

SECRET = "a-secret-that-only-the-server-knows"
OUT_OF_RANGE = [
    "99999999999999999999",
    "-99999999999999999999",
    "9999999999999",
    "253402300800",
    "100000000000000000",
    "-100000000000000000",
    "9000000000000000000",
]


def _body(payload: str) -> str:
    return b64encode(payload.encode("utf-8"), altchars=b"-_").decode("utf-8").rstrip("=")


def _hmac_token(payload: str) -> str:
    body = _body(payload)
    signature = b64encode(hmac.new(SECRET.encode("utf-8"), body.encode("utf-8"), sha256).digest(), altchars=b"-_")
    return body + "." + signature.decode("utf-8").rstrip("=")


def _legacy_token(payload: str) -> str:
    body = _body(payload)
    return body + b64encode(sha256((body + SECRET).encode("utf-8")).digest(), altchars=b"-_").decode("utf-8")[:10]


class _User:
    def __init__(self, user_id=42, created_at=None, sub_revoked_at=None):
        self.id = user_id
        self.username = f"user{user_id}"
        self.created_at = created_at or datetime.now(UTC) - timedelta(days=1)
        self.sub_revoked_at = sub_revoked_at


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


def _validate(token):
    return asyncio.run(BaseOperation(OperatorType.API).get_validated_sub(None, token))


def _refused(token) -> bool:
    try:
        _validate(token)
    except HTTPException as exc:
        assert exc.status_code == 404
        return True
    return False


def _out_of_range_tokens():
    tokens = []
    for ts in OUT_OF_RANGE:
        tokens += [
            pytest.param(_hmac_token(f"v4,42,{ts},0"), id=f"hmac-v4-{ts}"),
            pytest.param(_hmac_token(f"v3,42,{ts}"), id=f"hmac-v3-{ts}"),
            pytest.param(_hmac_token(f"v2,42,{ts}"), id=f"hmac-v2-{ts}"),
            pytest.param(_hmac_token(f"user42,{ts}"), id=f"hmac-two-part-{ts}"),
            pytest.param(_legacy_token(f"v4,42,{ts},0"), id=f"legacy-v4-{ts}"),
            pytest.param(_legacy_token(f"v3,42,{ts}"), id=f"legacy-v3-{ts}"),
            pytest.param(_legacy_token(f"user42,{ts}"), id=f"legacy-two-part-{ts}"),
        ]
    for iat in (-99999999999999999999, -(10**15), -(10**17), -9 * 10**18):
        token = pyjwt.encode({"sub": "user42", "access": "subscription", "iat": iat}, SECRET, algorithm="HS256")
        tokens.append(pytest.param(token, id=f"jwt-iat-{iat}"))
    return tokens


@pytest.mark.parametrize("token", _out_of_range_tokens())
def test_a_signed_token_whose_timestamp_is_out_of_range_is_refused_without_a_500(secret, store, token):
    assert asyncio.run(jwt_utils.get_subscription_payload(token)) is None
    assert _refused(token)


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(_hmac_token(f"v4,42,{int(datetime.now(UTC).timestamp())},0"), id="hmac-v4"),
        pytest.param(_legacy_token(f"v3,42,{int(datetime.now(UTC).timestamp())}"), id="legacy-v3"),
        pytest.param(
            pyjwt.encode(
                {"sub": "user42", "access": "subscription", "iat": int(datetime.now(UTC).timestamp())},
                SECRET,
                algorithm="HS256",
            ),
            id="jwt",
        ),
    ],
)
def test_an_in_range_signed_token_still_resolves(secret, store, token):
    assert _validate(token).id == 42


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(ConnectionRefusedError(111, "Connect call failed"), id="refused"),
        pytest.param(TimeoutError(), id="timeout"),
        pytest.param(OSError(65, "No route to host"), id="unreachable"),
        pytest.param(ValueError("bad secret row"), id="value"),
        pytest.param(OverflowError("overflow"), id="overflow"),
    ],
)
@pytest.mark.parametrize(
    "token",
    [
        pytest.param(_body("v4,42,1900000000,0") + "." + "A" * 43, id="hmac"),
        pytest.param(_body("v4,42,1900000000,0") + "abcdefghij", id="legacy"),
        pytest.param("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.signature", id="jwt"),
    ],
)
def test_an_error_while_reading_the_secret_still_raises(monkeypatch, store, token, error):
    async def _down():
        raise error

    monkeypatch.setattr(jwt_utils, "get_secret_key", _down)
    with pytest.raises(type(error)):
        asyncio.run(jwt_utils.get_subscription_payload(token))
    with pytest.raises(type(error)):
        _validate(token)
