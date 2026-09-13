import asyncio
from base64 import b64encode
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from fastapi import HTTPException

import app.operation as operation_module
from app.operation import BaseOperation, OperatorType
from app.utils import jwt as jwt_utils
from app.utils.jwt import get_subscription_payload

SECRET = "a-secret-that-only-the-server-knows"
OTHER_SECRET = "the-secret-after-it-was-rotated"


def _legacy_link(username: str, issued: datetime, secret: str, *, hex_signature: bool = False) -> str:
    payload = f"{username},{int(issued.timestamp())}"
    body = b64encode(payload.encode("utf-8"), altchars=b"-_").decode("utf-8").rstrip("=")
    digest = sha256((body + secret).encode("utf-8")).digest()
    signature = digest.hex()[:10] if hex_signature else b64encode(digest, altchars=b"-_").decode("utf-8")[:10]
    return body + signature


@pytest.fixture
def served_secret(monkeypatch):
    holder = {"value": SECRET}

    async def _secret():
        return holder["value"]

    monkeypatch.setattr(jwt_utils, "get_secret_key", _secret)
    return holder


class _StubUser:
    def __init__(self, username, created_at, sub_revoked_at=None):
        self.username = username
        self.created_at = created_at
        self.sub_revoked_at = sub_revoked_at


def _serve_user(monkeypatch, user):
    async def _get_user(db, username, **load_kwargs):
        return user if user and user.username == username else None

    monkeypatch.setattr(operation_module, "get_user", _get_user, raising=False)


def _read(token):
    return asyncio.run(get_subscription_payload(token))


def _resolve(token):
    op = BaseOperation(OperatorType.API)
    return asyncio.run(op.get_validated_sub(None, token))


def test_both_historic_signature_shapes_still_read(served_secret):
    issued = datetime.now(UTC).replace(microsecond=0)
    for hex_signature in (False, True):
        payload = _read(_legacy_link("old_customer", issued, SECRET, hex_signature=hex_signature))
        assert payload is not None
        assert payload["username"] == "old_customer"


def test_a_link_forged_without_the_secret_is_refused(served_secret):
    issued = datetime.now(UTC).replace(microsecond=0)
    genuine = _legacy_link("old_customer", issued, SECRET)
    forged = _legacy_link("old_customer", issued, "a-guess-at-the-secret")
    assert forged != genuine
    assert _read(forged) is None


def test_a_link_stops_reading_once_the_secret_is_rotated(served_secret):
    issued = datetime.now(UTC).replace(microsecond=0)
    link = _legacy_link("old_customer", issued, SECRET)
    assert _read(link) is not None
    served_secret["value"] = OTHER_SECRET
    assert _read(link) is None


def test_revoking_a_subscription_refuses_a_link_issued_before_it(served_secret, monkeypatch):
    issued = datetime.now(UTC).replace(microsecond=0) - timedelta(days=30)
    link = _legacy_link("old_customer", issued, SECRET)
    _serve_user(
        monkeypatch,
        _StubUser("old_customer", created_at=issued - timedelta(days=1), sub_revoked_at=datetime.now(UTC)),
    )
    with pytest.raises(HTTPException) as raised:
        _resolve(link)
    assert raised.value.status_code == 404


def test_a_link_issued_after_the_revocation_is_still_served(served_secret, monkeypatch):
    revoked_at = datetime.now(UTC) - timedelta(days=2)
    issued = datetime.now(UTC).replace(microsecond=0)
    link = _legacy_link("old_customer", issued, SECRET)
    _serve_user(
        monkeypatch,
        _StubUser("old_customer", created_at=revoked_at - timedelta(days=10), sub_revoked_at=revoked_at),
    )
    assert _resolve(link).username == "old_customer"


def test_a_link_older_than_the_account_itself_is_refused(served_secret, monkeypatch):
    issued = datetime.now(UTC).replace(microsecond=0) - timedelta(days=10)
    link = _legacy_link("old_customer", issued, SECRET)
    _serve_user(monkeypatch, _StubUser("old_customer", created_at=datetime.now(UTC)))
    with pytest.raises(HTTPException) as raised:
        _resolve(link)
    assert raised.value.status_code == 404
