import asyncio
from base64 import b64encode

import pytest
from fastapi import HTTPException

import app.operation as operation_module
from app.operation import BaseOperation, OperatorType
from app.utils import jwt as jwt_utils
from tests.release_jwt import load_release_jwt


def _body(payload: str) -> str:
    return b64encode(payload.encode("utf-8"), altchars=b"-_").decode("utf-8").rstrip("=")


TOKENS = {
    "hmac": _body("v4,42,1900000000,0") + "." + "A" * 43,
    "legacy": _body("v4,42,1900000000,0") + "abcdefghij",
    "jwt": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.signature",
}
OUTAGES = {
    "refused": lambda: ConnectionRefusedError(61, "Connect call failed"),
    "timeout": lambda: TimeoutError(),
    "unreachable": lambda: OSError(65, "No route to host"),
}


def _failing_secret(make_error):
    async def _secret():
        raise make_error()

    return _secret


@pytest.mark.parametrize("token_kind", sorted(TOKENS))
@pytest.mark.parametrize("outage", sorted(OUTAGES))
def test_a_database_outage_while_reading_the_secret_raises_exactly_as_the_release_does(monkeypatch, token_kind, outage):
    release = load_release_jwt("release_jwt_review_outage")
    monkeypatch.setattr(release, "get_secret_key", _failing_secret(OUTAGES[outage]))
    monkeypatch.setattr(jwt_utils, "get_secret_key", _failing_secret(OUTAGES[outage]))
    expected = type(OUTAGES[outage]())

    with pytest.raises(expected):
        asyncio.run(release.get_subscription_payload(TOKENS[token_kind]))
    with pytest.raises(expected):
        asyncio.run(jwt_utils.get_subscription_payload(TOKENS[token_kind]))


def test_a_database_outage_is_never_answered_as_a_missing_subscription(monkeypatch):
    monkeypatch.setattr(jwt_utils, "get_secret_key", _failing_secret(OUTAGES["refused"]))

    async def _lookup_must_not_run(*_args, **_kwargs):
        raise AssertionError("no user lookup happens without a decoded token")

    monkeypatch.setattr(operation_module, "get_user_by_id", _lookup_must_not_run)
    monkeypatch.setattr(operation_module, "get_user", _lookup_must_not_run)

    try:
        asyncio.run(BaseOperation(OperatorType.API).get_validated_sub(None, TOKENS["hmac"]))
    except HTTPException as exc:
        pytest.fail(f"a database outage was answered as HTTP {exc.status_code} {exc.detail!r}")
    except ConnectionRefusedError:
        pass
    else:
        pytest.fail("a database outage resolved a subscription")
