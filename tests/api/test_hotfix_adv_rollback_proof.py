import pytest

from tests.api import GetTestDB, client
from tests.api.helpers import auth_headers, create_user, delete_user, unique_name
from tests.release_jwt import load_release_jwt


@pytest.fixture
def release(monkeypatch):
    module = load_release_jwt("release_jwt_adv_proof")
    monkeypatch.setattr(module, "GetDB", GetTestDB)
    return module


def _status(token: str) -> int:
    return client.get(f"/sub/{token}/info").status_code


def _token(subscription_url: str) -> str:
    return subscription_url.rstrip("/").rsplit("/", 1)[-1]


@pytest.mark.asyncio
async def test_a_v4_and_a_v3_link_of_an_active_user_both_resolve_after_the_rollback(access_token, release):
    user = create_user(access_token, payload={"username": unique_name("adv_active")})
    try:
        v4 = await release.create_subscription_token(user["id"])
        v3 = _token(user["subscription_url"])
        assert _status(v4) == 200
        assert _status(v3) == 200
    finally:
        delete_user(access_token, user["username"])


@pytest.mark.asyncio
async def test_the_release_v4_link_of_a_revoked_user_is_refused_after_the_rollback(access_token, release):
    user = create_user(access_token, payload={"username": unique_name("adv_revoked")})
    try:
        stale = await release.create_subscription_token(user["id"])
        assert _status(stale) == 200
        response = client.post(f"/api/user/{user['username']}/revoke_sub", headers=auth_headers(access_token))
        assert response.status_code == 200, response.text
        assert _status(stale) == 404
        assert _status(_token(response.json()["subscription_url"])) == 200
    finally:
        delete_user(access_token, user["username"])


@pytest.mark.asyncio
async def test_a_v4_and_a_v3_link_of_a_deleted_user_are_refused_after_the_rollback(access_token, release):
    user = create_user(access_token, payload={"username": unique_name("adv_deleted")})
    v4 = await release.create_subscription_token(user["id"])
    v3 = _token(user["subscription_url"])
    assert _status(v4) == 200
    assert _status(v3) == 200
    delete_user(access_token, user["username"])
    assert _status(v4) == 404
    assert _status(v3) == 404
