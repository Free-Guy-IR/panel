import asyncio

from fastapi import status
from sqlalchemy import func, select

from app.db.models import Admin, APIKey
from app.models.api_key import APIKeyCreate
from tests.api import TestSession, client
from tests.api.api_key_actor import OPERATION_DONE, call_as_key
from tests.api.helpers import auth_headers, create_admin, delete_admin, strong_password, unique_name
from tests.api.test_bulk_delete_entities import count_admin_auth_rows, seed_admin_auth_rows

KEY_MANAGER = {"api_keys": {"create": True, "read": {"scope": 2}, "update": {"scope": 2}, "delete": {"scope": 2}}}
NARROW = {"api_keys": {"read": {"scope": 2}}}


def _login(username: str, password: str) -> str:
    response = client.post(
        "/api/admin/token", data={"username": username, "password": password, "grant_type": "password"}
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    return response.json()["access_token"]


def _create_key(session_token: str, permissions: dict | None = None) -> dict:
    body = {"name": unique_name("cleanup_key"), "inherit_permissions": permissions is None}
    if permissions is not None:
        body["permissions"] = permissions
    response = client.post("/api/api_key", headers=auth_headers(session_token), json=body)
    assert response.status_code == status.HTTP_201_CREATED, response.text
    return response.json()


async def _admin_exists(username: str) -> bool:
    async with TestSession() as db:
        return (await db.execute(select(func.count(Admin.id)).where(Admin.username == username))).scalar_one() > 0


async def _key_count(admin_id: int) -> int:
    async with TestSession() as db:
        return (await db.execute(select(func.count(APIKey.id)).where(APIKey.admin_id == admin_id))).scalar_one()


def test_an_api_key_deletes_an_admin_with_its_passkeys_but_cannot_create_its_replacement(access_token):
    holder = create_admin(access_token, role_id=2)
    victim = create_admin(access_token, role_id=3)
    replacement = unique_name("key_replacement")
    try:
        key = {"X-Api-Key": _create_key(_login(holder["username"], holder["password"]))["api_key"]}
        seed_admin_auth_rows(victim["id"])

        removed = client.delete(f"/api/admin/by-id/{victim['id']}", headers=key)
        assert removed.status_code == status.HTTP_204_NO_CONTENT, removed.text
        assert count_admin_auth_rows(victim["id"]) == {"passkeys": 0, "challenges": 0, "reminders": 0}

        made = client.post(
            "/api/admin",
            headers=key,
            json={"username": replacement, "password": strong_password("KeyReplacement"), "role_id": 3},
        )
        assert made.status_code == status.HTTP_401_UNAUTHORIZED, made.text
        assert not asyncio.run(_admin_exists(replacement))
    finally:
        delete_admin(access_token, holder["username"])
        if asyncio.run(_admin_exists(victim["username"])):
            delete_admin(access_token, victim["username"])


def test_a_custom_key_the_operation_lets_mint_and_rotate_still_needs_a_session(access_token):
    holder = create_admin(access_token, role_id=2)
    try:
        session = _login(holder["username"], holder["password"])
        custom = _create_key(session, KEY_MANAGER)
        narrower = {"name": unique_name("narrower"), "inherit_permissions": False, "permissions": NARROW}

        code, minted = call_as_key(custom["api_key"], "create_api_key", model=APIKeyCreate.model_validate(narrower))
        assert code == OPERATION_DONE
        code, _rotated = call_as_key(custom["api_key"], "revoke_api_key", key_id=minted.id)
        assert code == OPERATION_DONE
        keys_before = asyncio.run(_key_count(holder["id"]))

        key = {"X-Api-Key": custom["api_key"]}
        minted_over_http = client.post("/api/api_key", headers=key, json={**narrower, "name": unique_name("http")})
        rotated_over_http = client.post(f"/api/api_key/{minted.id}/revoke", headers=key)

        assert minted_over_http.status_code == status.HTTP_401_UNAUTHORIZED, minted_over_http.text
        assert rotated_over_http.status_code == status.HTTP_401_UNAUTHORIZED, rotated_over_http.text
        assert asyncio.run(_key_count(holder["id"])) == keys_before
    finally:
        delete_admin(access_token, holder["username"])
