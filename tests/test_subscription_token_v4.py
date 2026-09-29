import asyncio
import hmac
import importlib.util
import subprocess
from base64 import b64encode
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest
from fastapi import HTTPException

import app.operation as operation_module
from app.operation import BaseOperation, OperatorType
from app.utils import jwt as jwt_utils

ROOT = Path(__file__).resolve().parents[1]
RELEASE_COMMIT = "b3044efa2"
SECRET = "a-secret-that-only-the-server-knows"
CREATED = datetime(2026, 1, 1, tzinfo=UTC)


def _sign(payload: str, secret: str = SECRET) -> str:
    body = b64encode(payload.encode("utf-8"), altchars=b"-_").decode("utf-8").rstrip("=")
    signature = b64encode(hmac.new(secret.encode("utf-8"), body.encode("utf-8"), sha256).digest(), altchars=b"-_")
    return body + "." + signature.decode("utf-8").rstrip("=")


def _v4(user_id: int, issued: datetime, epoch: int, secret: str = SECRET) -> str:
    return _sign(f"v4,{user_id},{int(issued.timestamp())},{epoch}", secret)


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
    users = {}

    async def _get_user_by_id(_db, user_id, **_load_kwargs):
        return users.get(user_id)

    monkeypatch.setattr(operation_module, "get_user_by_id", _get_user_by_id)
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


def test_a_v4_token_reads_with_its_revocation_epoch(secret):
    issued = datetime.now(UTC).replace(microsecond=0)
    payload = asyncio.run(jwt_utils.get_subscription_payload(_v4(42, issued, 1_900_000_001)))
    assert payload == {"user_id": 42, "created_at": issued, "revocation_epoch": 1_900_000_001}


@pytest.mark.parametrize(
    "payload",
    [
        "v4,42,1900000000",
        "v4,42,1900000000,x",
        "v4,x,1900000000,0",
        "v4,42,soon,0",
        "v4,42,1900000000,0,1",
        "v5,42,1,0",
    ],
)
def test_a_malformed_v4_payload_is_refused(secret, payload):
    assert asyncio.run(jwt_utils.get_subscription_payload(_sign(payload))) is None


def test_a_v4_token_signed_with_another_secret_is_refused(secret):
    token = _v4(42, datetime.now(UTC), 0, secret="a-guess-at-the-secret")
    assert asyncio.run(jwt_utils.get_subscription_payload(token)) is None


def test_the_revocation_epoch_rounds_to_the_nearest_second_and_reads_naive_values_as_utc():
    assert jwt_utils.revocation_epoch(None) == 0
    instant = datetime(2026, 9, 25, 10, 0, 0, 499_999, tzinfo=UTC)
    assert jwt_utils.revocation_epoch(instant) == int(instant.replace(microsecond=0).timestamp())
    assert jwt_utils.revocation_epoch(instant.replace(microsecond=500_000)) == int(instant.timestamp()) + 1
    assert jwt_utils.revocation_epoch(instant.replace(tzinfo=None)) == jwt_utils.revocation_epoch(instant)


def test_a_v4_token_resolves_only_while_its_epoch_is_the_users_current_epoch(secret, store):
    revoked = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
    store[42] = _User(sub_revoked_at=revoked)
    issued = revoked + timedelta(minutes=5)

    assert _resolve(_v4(42, issued, jwt_utils.revocation_epoch(revoked))).id == 42
    assert _refused(_v4(42, issued, jwt_utils.revocation_epoch(revoked) - 1))
    assert _refused(_v4(42, issued, 0))
    assert _refused(_v4(42, issued, jwt_utils.revocation_epoch(revoked) + 1))


def test_a_v4_token_of_a_never_revoked_user_resolves_until_the_first_revoke(secret, store):
    store[42] = _User()
    token = _v4(42, datetime.now(UTC), 0)
    assert _resolve(token).id == 42

    store[42].sub_revoked_at = datetime.now(UTC)
    assert _refused(token)


def test_a_v4_token_older_than_the_account_or_for_a_missing_user_is_refused(secret, store):
    store[42] = _User(created_at=datetime.now(UTC))
    assert _refused(_v4(42, datetime.now(UTC) - timedelta(days=1), 0))
    assert _refused(_v4(7, datetime.now(UTC), 0))


def test_v3_tokens_keep_the_time_rule(secret, store):
    issued = datetime.now(UTC).replace(microsecond=0)
    token = _sign(f"v3,42,{int(issued.timestamp())}")
    store[42] = _User(sub_revoked_at=issued - timedelta(days=1))
    assert _resolve(token).id == 42
    store[42].sub_revoked_at = issued + timedelta(seconds=30)
    assert _refused(token)


def _release_jwt(tmp_path):
    source = subprocess.run(
        ["git", "show", f"{RELEASE_COMMIT}:app/utils/jwt.py"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    if source.returncode:
        pytest.skip(f"release commit {RELEASE_COMMIT} is not in this checkout")
    path = tmp_path / "release_jwt.py"
    path.write_text(source.stdout)
    spec = importlib.util.spec_from_file_location("release_jwt", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def release(tmp_path, monkeypatch):
    module = _release_jwt(tmp_path)

    async def _secret():
        return SECRET

    monkeypatch.setattr(module, "get_secret_key", _secret)
    return module


def test_a_link_the_release_minted_opens_after_the_rollback(secret, store, release):
    store[42] = _User()
    token = asyncio.run(release.create_subscription_token(42, None))
    assert asyncio.run(release.get_subscription_payload(token))["revocation_epoch"] == 0
    assert _resolve(token).id == 42


def test_a_release_link_revoked_and_reminted_on_the_release_keeps_only_the_newest_link(secret, store, release):
    first_revoke = release.next_revocation_instant(None)
    second_revoke = release.next_revocation_instant(first_revoke)
    store[42] = _User(sub_revoked_at=first_revoke)
    before = asyncio.run(release.create_subscription_token(42, first_revoke))
    assert _resolve(before).id == 42

    store[42].sub_revoked_at = second_revoke
    after = asyncio.run(release.create_subscription_token(42, second_revoke))

    assert _refused(before)
    assert _resolve(after).id == 42
    assert jwt_utils.revocation_epoch(second_revoke) == jwt_utils.revocation_epoch(first_revoke) + 1


def test_a_revoke_on_the_rollback_image_kills_every_release_link(secret, store, release):
    released_revoke = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=1)
    store[42] = _User(sub_revoked_at=released_revoke)
    release_link = asyncio.run(release.create_subscription_token(42, released_revoke))
    never_revoked_link = asyncio.run(release.create_subscription_token(42, None))
    assert _resolve(release_link).id == 42
    assert _refused(never_revoked_link)

    store[42].sub_revoked_at = released_revoke + timedelta(minutes=50, microseconds=123_456)
    rollback_link = asyncio.run(jwt_utils.create_subscription_token(42))

    assert _refused(release_link)
    assert _resolve(rollback_link).id == 42


def test_the_release_and_the_rollback_image_agree_on_every_token(secret, store, release):
    revoked = datetime(2026, 9, 25, 9, 30, 15, 700_000, tzinfo=UTC)
    tokens = [
        asyncio.run(release.create_subscription_token(42, value))
        for value in (None, revoked, revoked - timedelta(seconds=1), revoked + timedelta(seconds=1))
    ]
    for sub_revoked_at in (None, revoked, revoked.replace(tzinfo=None), revoked + timedelta(seconds=2)):
        store[42] = _User(sub_revoked_at=sub_revoked_at)
        for token in tokens:
            released = asyncio.run(release.get_subscription_payload(token))
            expected = released["revocation_epoch"] == release.revocation_epoch(sub_revoked_at)
            assert asyncio.run(jwt_utils.get_subscription_payload(token)) == released
            assert _refused(token) is not expected


def test_a_revoke_racing_a_release_mint_and_a_validation_never_serves_a_stale_link(secret, store, release, monkeypatch):
    first_revoke = release.next_revocation_instant(None)
    store[42] = _User(sub_revoked_at=first_revoke)
    stale_link = asyncio.run(release.create_subscription_token(42, first_revoke))
    rollback_revoke = first_revoke + timedelta(seconds=5, microseconds=250_000)

    async def race(read_before_revoke: bool):
        read_done = asyncio.Event()
        revoke_done = asyncio.Event()
        seen = {}

        async def stored_revoked_at(user_id):
            if not read_before_revoke:
                await asyncio.wait_for(revoke_done.wait(), 5)
            value = store[user_id].sub_revoked_at
            read_done.set()
            return value

        async def lookup(_db, user_id, **_load_kwargs):
            if not read_before_revoke:
                await asyncio.wait_for(revoke_done.wait(), 5)
            user = _User(sub_revoked_at=store[user_id].sub_revoked_at)
            read_done.set()
            return user

        async def revoke():
            if read_before_revoke:
                await asyncio.wait_for(read_done.wait(), 5)
            store[42].sub_revoked_at = rollback_revoke
            revoke_done.set()

        monkeypatch.setattr(release, "_stored_sub_revoked_at", stored_revoked_at)
        minted, _ = await asyncio.gather(release.create_subscription_token(42), revoke())
        seen["minted"] = minted

        read_done.clear()
        revoke_done.clear()
        store[42].sub_revoked_at = first_revoke
        monkeypatch.setattr(operation_module, "get_user_by_id", lookup)
        outcome, _ = await asyncio.gather(
            BaseOperation(OperatorType.API).get_validated_sub(None, stale_link), revoke(), return_exceptions=True
        )
        seen["validation"] = outcome
        return seen

    before = asyncio.run(race(read_before_revoke=True))
    after = asyncio.run(race(read_before_revoke=False))

    assert before["validation"].id == 42
    assert isinstance(after["validation"], HTTPException) and after["validation"].status_code == 404

    async def final(_db, user_id, **_load_kwargs):
        return store.get(user_id)

    monkeypatch.setattr(operation_module, "get_user_by_id", final)
    store[42] = _User(sub_revoked_at=rollback_revoke)
    assert _refused(before["minted"])
    assert _refused(stale_link)
    assert _resolve(after["minted"]).id == 42
