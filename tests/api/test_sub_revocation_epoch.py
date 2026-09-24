import time
from base64 import b64encode
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from math import ceil
from urllib.parse import unquote

import pytest
from sqlalchemy import event, select

from app.db.models import User
from app.utils import jwt as jwt_utils
from app.utils.jwt import get_secret_key, next_revocation_instant, revocation_epoch
from tests.api import TestSession, client, engine
from tests.api.helpers import auth_headers, create_user, delete_user, unique_name


def _token(subscription_url: str) -> str:
    return subscription_url.rstrip("/").rsplit("/", 1)[-1]


def _revoke(access_token: str, username: str) -> dict:
    response = client.post(f"/api/user/{username}/revoke_sub", headers=auth_headers(access_token))
    assert response.status_code == 200, response.text
    return response.json()


async def _stored_revoked_at(user_id: int) -> datetime | None:
    async with TestSession() as db:
        return (await db.execute(select(User.sub_revoked_at).where(User.id == user_id))).scalar_one()


def _legacy_signed(payload: str, secret: str) -> str:
    body = b64encode(payload.encode(), altchars=b"-_").decode().rstrip("=")
    return body + b64encode(sha256((body + secret).encode()).digest(), altchars=b"-_").decode()[:10]


def test_a_token_minted_through_apps_in_the_revoke_second_dies_with_the_revoke(access_token, monkeypatch):
    user = create_user(access_token, payload={"username": unique_name("apps_poll"), "hwid_limit": 0})
    try:
        leaked = _token(user["subscription_url"])
        minted_at = time.time() + 0.9
        with monkeypatch.context() as frozen_clock:
            frozen_clock.setattr(jwt_utils.time, "time", lambda: minted_at)
            apps = client.get(f"/sub/{leaked}/apps")
        assert apps.status_code == 200
        refreshed = {_token(unquote(app["import_url"])) for app in apps.json() if "/sub/" in unquote(app["import_url"])}
        assert refreshed, apps.json()
        for token in refreshed:
            assert client.get(f"/sub/{token}/info").status_code == 200

        _revoke(access_token, user["username"])

        for token in refreshed:
            assert client.get(f"/sub/{token}/info").status_code == 404, "a self-minted token outlived the revoke"
        assert client.get(f"/sub/{leaked}/info").status_code == 404
    finally:
        delete_user(access_token, user["username"])


def test_the_link_returned_by_the_revoke_works(access_token):
    user = create_user(access_token, payload={"username": unique_name("revoke_link")})
    try:
        revoked = _revoke(access_token, user["username"])
        assert client.get(f"/sub/{_token(revoked['subscription_url'])}/info").status_code == 200
        again = client.get(f"/api/user/{user['username']}", headers=auth_headers(access_token)).json()
        assert client.get(f"/sub/{_token(again['subscription_url'])}/info").status_code == 200
    finally:
        delete_user(access_token, user["username"])


@pytest.mark.asyncio
async def test_two_revokes_in_one_second_get_distinct_epochs_and_the_first_link_dies(access_token):
    user = create_user(access_token, payload={"username": unique_name("double_revoke")})
    try:
        first = _revoke(access_token, user["username"])
        first_epoch = revocation_epoch(await _stored_revoked_at(user["id"]))
        second = _revoke(access_token, user["username"])
        second_epoch = revocation_epoch(await _stored_revoked_at(user["id"]))
        assert second_epoch > first_epoch
        assert client.get(f"/sub/{_token(first['subscription_url'])}/info").status_code == 404
        assert client.get(f"/sub/{_token(second['subscription_url'])}/info").status_code == 200
    finally:
        delete_user(access_token, user["username"])


def test_the_next_revocation_instant_is_a_whole_second_after_the_previous_one():
    now = datetime.now(UTC)
    ahead = (now + timedelta(seconds=5)).replace(microsecond=400_000)
    following = next_revocation_instant(ahead)
    assert following.microsecond == 0
    assert revocation_epoch(following) == revocation_epoch(ahead) + 1
    assert next_revocation_instant(None).microsecond == 0
    assert next_revocation_instant(None) <= now + timedelta(seconds=1)
    assert revocation_epoch(ahead.replace(tzinfo=None)) == revocation_epoch(ahead)


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["v2", "v3"])
async def test_v2_and_v3_tokens_keep_the_time_rule(access_token, version):
    user = create_user(access_token, payload={"username": unique_name(f"legacy_{version}")})
    try:
        secret = await get_secret_key()
        before = _legacy_signed(f"{version},{user['id']},{ceil(time.time()) + 2}", secret)
        assert client.get(f"/sub/{before}/info").status_code == 200

        async with TestSession() as db:
            db_user = await db.get(User, user["id"])
            db_user.sub_revoked_at = datetime.now(UTC) + timedelta(seconds=30)
            await db.commit()

        assert client.get(f"/sub/{before}/info").status_code == 404
        after = _legacy_signed(f"{version},{user['id']},{ceil(time.time()) + 60}", secret)
        assert client.get(f"/sub/{after}/info").status_code == 200
    finally:
        delete_user(access_token, user["username"])


def test_listing_users_with_links_does_not_query_each_users_revocation(access_token):
    users = [create_user(access_token, payload={"username": unique_name("list_sub")}) for _ in range(3)]
    lookups = []

    def _record(*args):
        statement = args[2]
        if statement.lstrip().upper().startswith("SELECT USERS.SUB_REVOKED_AT"):
            lookups.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        response = client.get(
            "/api/users", params={"load_sub": "true", "limit": 50}, headers=auth_headers(access_token)
        )
        assert response.status_code == 200
        listed = [u for u in response.json()["users"] if u["username"] in {user["username"] for user in users}]
        assert len(listed) == 3
        assert all(u["subscription_url"] for u in listed)
        assert lookups == []
        for listed_user in listed:
            assert client.get(f"/sub/{_token(listed_user['subscription_url'])}/info").status_code == 200
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _record)
        for user in users:
            delete_user(access_token, user["username"])
