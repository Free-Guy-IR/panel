import asyncio

import pytest
from fastapi import status
from sqlalchemy import func, select

from app.db.models import Admin, APIKey
from tests.api import TestSession, client
from tests.api.concurrent_requests import send_concurrently
from tests.api.helpers import auth_headers, create_admin, delete_admin, strong_password, unique_name

KEY_HEADER_FORMS = ("X-Api-Key", "Authorization")
MODIFY_PATHS = ("/api/admin/{username}", "/api/admin/by-username/{username}", "/api/admin/by-id/{id}")
KEY_MANAGER = {
    "api_keys": {"create": True, "read": {"scope": 2}, "update": {"scope": 2}, "delete": {"scope": 2}},
    "admins": {"read": True, "create": True, "update": True},
}
NARROW_KEY = {"api_keys": {"read": {"scope": 2}}}


def _login(username: str, password: str) -> str:
    response = client.post(
        "/api/admin/token", data={"username": username, "password": password, "grant_type": "password"}
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    return response.json()["access_token"]


def _can_login(username: str, password: str) -> bool:
    response = client.post(
        "/api/admin/token", data={"username": username, "password": password, "grant_type": "password"}
    )
    return response.status_code == status.HTTP_200_OK


def _key_headers(raw_key: str, form: str) -> dict[str, str]:
    return {"X-Api-Key": raw_key} if form == "X-Api-Key" else {"Authorization": f"ApiKey {raw_key}"}


def _create_key(session_token: str, permissions: dict | None = None) -> dict:
    body = {"name": unique_name("cred_key"), "inherit_permissions": permissions is None}
    if permissions is not None:
        body["permissions"] = permissions
    response = client.post("/api/api_key", headers=auth_headers(session_token), json=body)
    assert response.status_code == status.HTTP_201_CREATED, response.text
    return response.json()


async def _admin_row(admin_id: int) -> tuple[str, int | None]:
    async with TestSession() as db:
        row = (await db.execute(select(Admin).where(Admin.id == admin_id))).scalar_one()
        return row.hashed_password, row.telegram_id


async def _key_rows(admin_id: int) -> list[tuple]:
    async with TestSession() as db:
        rows = await db.execute(
            select(APIKey.id, APIKey.name, APIKey.status, APIKey.key_hash, APIKey.inherit_permissions)
            .where(APIKey.admin_id == admin_id)
            .order_by(APIKey.id)
        )
        return [tuple(row) for row in rows.all()]


async def _admin_count() -> int:
    async with TestSession() as db:
        return (await db.execute(select(func.count(Admin.id)))).scalar_one()


@pytest.fixture
def holder(access_token):
    admin = create_admin(access_token, role_id=2)
    admin["session"] = _login(admin["username"], admin["password"])
    admin["key"] = _create_key(admin["session"])["api_key"]
    yield admin
    delete_admin(access_token, admin["username"])


@pytest.fixture
def victim(access_token):
    admin = create_admin(access_token, role_id=3)
    yield admin
    delete_admin(access_token, admin["username"])


@pytest.mark.parametrize("form", KEY_HEADER_FORMS)
@pytest.mark.parametrize("field", ["password", "telegram_id"])
def test_an_api_key_cannot_set_a_password_or_telegram_id_on_another_admin(holder, victim, form, field):
    body = {"password": strong_password("PlantedAdmin")} if field == "password" else {"telegram_id": 424242}
    before = asyncio.run(_admin_row(victim["id"]))
    for path in MODIFY_PATHS:
        response = client.put(path.format(**victim), headers=_key_headers(holder["key"], form), json=body)
        assert response.status_code == status.HTTP_403_FORBIDDEN, (path, response.text)
    assert asyncio.run(_admin_row(victim["id"])) == before
    if "password" in body:
        assert not _can_login(victim["username"], body["password"])


def test_an_api_key_still_changes_other_admin_fields_and_resubmits_an_unchanged_telegram_id(holder, victim):
    session_set = client.put(
        f"/api/admin/by-id/{victim['id']}", headers=auth_headers(holder["session"]), json={"telegram_id": 616161}
    )
    assert session_set.status_code == status.HTTP_200_OK, session_set.text
    response = client.put(
        f"/api/admin/by-id/{victim['id']}",
        headers={"X-Api-Key": holder["key"]},
        json={"note": "set by a key", "telegram_id": 616161},
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["note"] == "set by a key"


def test_a_session_still_sets_another_admins_password_and_telegram_id(holder, victim):
    new_password = strong_password("SessionSetAdmin")
    response = client.put(
        f"/api/admin/{victim['username']}",
        headers=auth_headers(holder["session"]),
        json={"password": new_password, "telegram_id": 515151},
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    assert _can_login(victim["username"], new_password)
    assert asyncio.run(_admin_row(victim["id"]))[1] == 515151


@pytest.mark.parametrize("form", KEY_HEADER_FORMS)
def test_an_api_key_cannot_create_an_admin(holder, form):
    username = unique_name("key_made_admin")
    password = strong_password("KeyMadeAdmin")
    before = asyncio.run(_admin_count())
    response = client.post(
        "/api/admin",
        headers=_key_headers(holder["key"], form),
        json={"username": username, "password": password, "role_id": 3},
    )
    assert response.status_code == status.HTTP_401_UNAUTHORIZED, response.text
    assert asyncio.run(_admin_count()) == before
    assert not _can_login(username, password)


def test_a_session_still_creates_an_admin(access_token, holder):
    username = unique_name("session_made_admin")
    password = strong_password("SessionMadeAdmin")
    response = client.post(
        "/api/admin",
        headers=auth_headers(holder["session"]),
        json={"username": username, "password": password, "role_id": 3},
    )
    assert response.status_code == status.HTTP_201_CREATED, response.text
    try:
        assert _can_login(username, password)
    finally:
        delete_admin(access_token, username)


@pytest.mark.parametrize("kind", ["inheriting", "custom"])
@pytest.mark.parametrize("form", KEY_HEADER_FORMS)
def test_an_api_key_cannot_create_modify_rotate_or_delete_api_keys(holder, kind, form):
    custom = kind == "custom"
    acting = _create_key(holder["session"], KEY_MANAGER if custom else None)
    target = _create_key(holder["session"], NARROW_KEY if custom else None)
    other = _create_key(holder["session"], NARROW_KEY if custom else None)
    mint = {"name": unique_name("minted"), "inherit_permissions": not custom}
    if custom:
        mint["permissions"] = NARROW_KEY
    before = asyncio.run(_key_rows(holder["id"]))
    headers = _key_headers(acting["api_key"], form)

    attempts = {
        "create": client.post("/api/api_key", headers=headers, json=mint),
        "modify": client.patch(f"/api/api_key/{target['id']}", headers=headers, json={"name": unique_name("renamed")}),
        "modify-self": client.patch(f"/api/api_key/{acting['id']}", headers=headers, json={"name": unique_name("me")}),
        "rotate": client.post(f"/api/api_key/{target['id']}/revoke", headers=headers),
        "delete": client.delete(f"/api/api_key/{target['id']}", headers=headers),
        "bulk-delete": client.post("/api/api_keys/bulk/delete", headers=headers, json={"ids": [other["id"]]}),
    }

    codes = {name: response.status_code for name, response in attempts.items()}
    assert codes == dict.fromkeys(attempts, status.HTTP_401_UNAUTHORIZED)
    assert asyncio.run(_key_rows(holder["id"])) == before
    assert client.get("/api/api_keys", headers={"X-Api-Key": target["api_key"]}).status_code == status.HTTP_200_OK


def test_an_api_key_still_lists_and_reads_api_keys(holder):
    target = _create_key(holder["session"], None)
    listed = client.get("/api/api_keys", headers={"X-Api-Key": holder["key"]})
    assert listed.status_code == status.HTTP_200_OK, listed.text
    read = client.get(f"/api/api_key/{target['id']}", headers={"X-Api-Key": holder["key"]})
    assert read.status_code == status.HTTP_200_OK, read.text


def test_a_session_still_creates_modifies_rotates_and_deletes_api_keys(holder):
    session = auth_headers(holder["session"])
    created = _create_key(holder["session"], None)
    renamed = client.patch(f"/api/api_key/{created['id']}", headers=session, json={"name": unique_name("renamed")})
    assert renamed.status_code == status.HTTP_200_OK, renamed.text
    rotated = client.post(f"/api/api_key/{created['id']}/revoke", headers=session)
    assert rotated.status_code == status.HTTP_200_OK, rotated.text
    assert rotated.json()["api_key"] != created["api_key"]
    removed = client.delete(f"/api/api_key/{created['id']}", headers=session)
    assert removed.status_code == status.HTTP_204_NO_CONTENT, removed.text
    other = _create_key(holder["session"], None)
    bulk = client.post("/api/api_keys/bulk/delete", headers=session, json={"ids": [other["id"]]})
    assert bulk.status_code == status.HTTP_200_OK, bulk.text


def test_an_invalid_session_next_to_a_valid_key_cannot_change_credentials(holder, victim):
    headers = {"Authorization": "Bearer not-a-real-token", "X-Api-Key": holder["key"]}
    before = asyncio.run(_admin_row(victim["id"]))
    password = client.put(
        f"/api/admin/{victim['username']}", headers=headers, json={"password": strong_password("MixedAdmin")}
    )
    assert password.status_code == status.HTTP_403_FORBIDDEN, password.text
    minted = client.post("/api/api_key", headers=headers, json={"name": unique_name("mixed")})
    assert minted.status_code == status.HTTP_401_UNAUTHORIZED, minted.text
    made = client.post(
        "/api/admin",
        headers=headers,
        json={"username": unique_name("mixed_admin"), "password": strong_password("MixedMade"), "role_id": 3},
    )
    assert made.status_code == status.HTTP_401_UNAUTHORIZED, made.text
    assert asyncio.run(_admin_row(victim["id"])) == before


def test_concurrent_key_credential_changes_all_fail_while_the_sessions_land(holder, victim):
    session_password = strong_password("SessionRaced")
    key_passwords = [strong_password(f"KeyRaced{index}") for index in range(3)]
    key = {"X-Api-Key": holder["key"]}
    victim_url = f"/api/admin/by-id/{victim['id']}"
    keys_before = len(asyncio.run(_key_rows(holder["id"])))

    calls = [("PUT", victim_url, {"headers": key, "json": {"password": password}}) for password in key_passwords]
    calls += [("POST", "/api/api_key", {"headers": key, "json": {"name": unique_name("raced")}}) for _ in range(3)]
    calls.append(
        ("PUT", victim_url, {"headers": auth_headers(holder["session"]), "json": {"password": session_password}})
    )
    calls.append(
        ("POST", "/api/api_key", {"headers": auth_headers(holder["session"]), "json": {"name": unique_name("s")}})
    )
    *key_password_codes, key_mint_a, key_mint_b, key_mint_c, session_password_code, session_mint = [
        response.status_code for response in send_concurrently(calls)
    ]

    assert key_password_codes == [status.HTTP_403_FORBIDDEN] * len(key_passwords)
    assert [key_mint_a, key_mint_b, key_mint_c] == [status.HTTP_401_UNAUTHORIZED] * 3
    assert session_password_code == status.HTTP_200_OK
    assert session_mint == status.HTTP_201_CREATED
    assert _can_login(victim["username"], session_password)
    for password in key_passwords:
        assert not _can_login(victim["username"], password)
    assert len(asyncio.run(_key_rows(holder["id"]))) == keys_before + 1
