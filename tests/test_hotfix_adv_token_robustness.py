import asyncio
import hmac
import os
import time
from base64 import b64encode
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import jwt as pyjwt
import pytest
from fastapi import HTTPException

import app.operation as operation_module
from app.operation import BaseOperation, OperatorType
from app.utils import jwt as jwt_utils
from tests.release_jwt import load_release_jwt

SECRET = "a-secret-that-only-the-server-knows"
CREATED = datetime(2026, 1, 1, tzinfo=UTC)


def _sign(payload: str, secret: str = SECRET) -> str:
    body = b64encode(payload.encode("utf-8"), altchars=b"-_").decode("utf-8").rstrip("=")
    signature = b64encode(hmac.new(secret.encode("utf-8"), body.encode("utf-8"), sha256).digest(), altchars=b"-_")
    return body + "." + signature.decode("utf-8").rstrip("=")


class _User:
    def __init__(self, user_id=42, created_at=CREATED, sub_revoked_at=None):
        self.id = user_id
        self.username = f"user{user_id}"
        self.created_at = created_at
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
        for user in users.values():
            if user.username == username:
                return user
        return None

    monkeypatch.setattr(operation_module, "get_user_by_id", _get_user_by_id)
    monkeypatch.setattr(operation_module, "get_user", _get_user)
    return users


def _resolve(token):
    return asyncio.run(BaseOperation(OperatorType.API).get_validated_sub(None, token))


def _refused(token) -> bool:
    try:
        _resolve(token)
    except HTTPException as exc:
        assert exc.status_code == 404
        return True
    return False


@pytest.mark.parametrize(
    "created_at_field",
    [
        "99999999999999999999",
        "-99999999999999999999",
        "9999999999999",
        "253402300800",
    ],
)
def test_a_signed_v4_token_with_an_out_of_range_timestamp_is_refused_without_a_500(secret, created_at_field):
    token = _sign(f"v4,42,{created_at_field},0")
    assert asyncio.run(jwt_utils.get_subscription_payload(token)) is None


def test_a_signed_v4_token_with_an_out_of_range_timestamp_does_not_500_the_route(secret, store):
    token = _sign("v4,42,99999999999999999999,0")
    assert _refused(token)


@pytest.mark.parametrize(
    "error",
    [ConnectionRefusedError(111, "Connect call failed"), OSError("db down"), ValueError("bad"), OverflowError("x")],
)
@pytest.mark.parametrize(
    "token",
    [
        _sign("v4,42,1900000000,0"),
        "djQsNDIsMTkwMDAwMDAwMCww" + "abcdefghij",
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ1c2VyNDIifQ.c2ln",
    ],
)
def test_an_error_while_reading_the_secret_propagates_as_in_the_release(monkeypatch, token, error):
    release = load_release_jwt("release_jwt_secret_errors")

    async def _down():
        raise error

    monkeypatch.setattr(jwt_utils, "get_secret_key", _down)
    monkeypatch.setattr(release, "get_secret_key", _down)
    with pytest.raises(type(error)):
        asyncio.run(release.get_subscription_payload(token))
    with pytest.raises(type(error)):
        asyncio.run(jwt_utils.get_subscription_payload(token))


def test_a_signed_v3_token_with_an_out_of_range_timestamp_is_refused_without_a_500(secret):
    assert asyncio.run(jwt_utils.get_subscription_payload(_sign("v3,42,99999999999999999999"))) is None


def _legacy_signed(payload: str, hex_signature: bool) -> str:
    body = b64encode(payload.encode("utf-8"), altchars=b"-_").decode("utf-8").rstrip("=")
    digest = sha256((body + SECRET).encode("utf-8")).digest()
    return body + (digest.hex()[:10] if hex_signature else b64encode(digest, altchars=b"-_").decode("utf-8")[:10])


@pytest.mark.parametrize("version", ["v2", "v3"])
def test_an_hmac_v2_or_v3_link_of_an_active_user_still_resolves(secret, store, version):
    issued = datetime.now(UTC).replace(microsecond=0)
    store[42] = _User(created_at=issued - timedelta(days=1))
    assert _resolve(_sign(f"{version},42,{int(issued.timestamp())}")).id == 42


@pytest.mark.parametrize("hex_signature", [True, False])
@pytest.mark.parametrize("version", ["v2", "v3"])
def test_a_legacy_signed_v2_or_v3_link_of_an_active_user_still_resolves(secret, store, version, hex_signature):
    issued = datetime.now(UTC).replace(microsecond=0)
    store[42] = _User(created_at=issued - timedelta(days=1))
    assert _resolve(_legacy_signed(f"{version},42,{int(issued.timestamp())}", hex_signature)).id == 42


@pytest.mark.parametrize("hex_signature", [True, False])
def test_a_legacy_signed_link_with_a_wrong_signature_is_refused(secret, store, hex_signature):
    issued = datetime.now(UTC).replace(microsecond=0)
    store[42] = _User(created_at=issued - timedelta(days=1))
    token = _legacy_signed(f"v3,42,{int(issued.timestamp())}", hex_signature)
    forged = token[:-1] + ("0" if token[-1] != "0" else "1")
    assert _refused(forged)


def test_a_two_part_legacy_token_of_an_active_user_still_resolves(secret, store):
    issued = datetime.now(UTC).replace(microsecond=0)
    store[42] = _User(user_id=42, created_at=issued - timedelta(days=1))
    token = _sign(f"user42,{int(issued.timestamp())}")
    assert _resolve(token).username == "user42"


def test_a_legacy_jwt_subscription_token_of_an_active_user_still_resolves(secret, store):
    issued = datetime.now(UTC).replace(microsecond=0)
    store[42] = _User(user_id=42, created_at=issued - timedelta(days=1))
    payload = {"sub": "user42", "access": "subscription", "iat": int(issued.timestamp())}
    token = pyjwt.encode(payload, SECRET, algorithm="HS256")
    assert _resolve(token).username == "user42"


def test_a_legacy_jwt_with_an_out_of_range_iat_is_refused_without_a_500(secret, store):
    payload = {"sub": "user42", "access": "subscription", "iat": 99999999999999999999}
    token = pyjwt.encode(payload, SECRET, algorithm="HS256")
    assert _refused(token)


@pytest.mark.parametrize("iat", [-99999999999999999999, -(10**15)])
def test_a_signed_legacy_jwt_whose_iat_is_out_of_range_is_refused_without_a_500(secret, store, iat):
    token = pyjwt.encode({"sub": "user42", "access": "subscription", "iat": iat}, SECRET, algorithm="HS256")
    assert asyncio.run(jwt_utils.get_subscription_payload(token)) is None
    assert _refused(token)


def _legacy_body(payload: str) -> str:
    return b64encode(payload.encode("utf-8"), altchars=b"-_").decode("utf-8").rstrip("=")


@pytest.mark.parametrize("signature", ["é" * 10, "abcdefghié", "١" * 10])
def test_a_legacy_format_v4_token_with_a_non_ascii_signature_is_refused_without_a_500(secret, store, signature):
    token = _legacy_body("v4,42,1900000000,0") + signature
    assert asyncio.run(jwt_utils.get_subscription_payload(token)) is None
    assert _refused(token)


@pytest.fixture(params=["Asia/Tehran", "America/Los_Angeles", "UTC"])
def process_tz(request):
    saved = os.environ.get("TZ")
    os.environ["TZ"] = request.param
    time.tzset()
    yield request.param
    if saved is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = saved
    time.tzset()


def _naive_utc(instant: datetime) -> datetime:
    return instant.astimezone(UTC).replace(tzinfo=None)


def _v4(user_id: int, issued: datetime, epoch: int) -> str:
    return _sign(f"v4,{user_id},{int(issued.timestamp())},{epoch}")


def test_a_fresh_users_v4_link_opens_whatever_the_process_timezone(secret, store, process_tz):
    now = datetime.now(UTC).replace(microsecond=0)
    store[42] = _User(created_at=_naive_utc(now - timedelta(seconds=60)))
    assert _resolve(_v4(42, now, 0)).id == 42


def test_a_v4_link_minted_before_the_account_existed_is_refused_whatever_the_process_timezone(
    secret, store, process_tz
):
    now = datetime.now(UTC).replace(microsecond=0)
    store[42] = _User(created_at=_naive_utc(now))
    assert _refused(_v4(42, now - timedelta(hours=1), 0))


def test_a_v3_link_minted_before_a_revoke_is_refused_whatever_the_process_timezone(secret, store, process_tz):
    now = datetime.now(UTC).replace(microsecond=0)
    store[42] = _User(created_at=_naive_utc(now - timedelta(days=1)), sub_revoked_at=_naive_utc(now))
    assert _refused(_sign(f"v3,42,{int((now - timedelta(hours=1)).timestamp())}"))


def test_a_v3_link_minted_after_a_revoke_opens_whatever_the_process_timezone(secret, store, process_tz):
    now = datetime.now(UTC).replace(microsecond=0)
    store[42] = _User(
        created_at=_naive_utc(now - timedelta(days=1)), sub_revoked_at=_naive_utc(now - timedelta(hours=1))
    )
    assert _resolve(_sign(f"v3,42,{int(now.timestamp())}")).id == 42
