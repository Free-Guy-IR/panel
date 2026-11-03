import asyncio

from fastapi import status
from sqlalchemy import select

from app.db.models import Admin
from app.models.admin import hash_password
from tests.api import TestSession, client
from tests.api.concurrent_requests import send_concurrently
from tests.api.helpers import auth_headers, strong_password, unique_name

ADMIN_EDITOR = {"admins": {"read": True, "update": True}}


def _create_owner() -> dict:
    username = unique_name("selfedit_owner")
    password = strong_password("SelfEditOwner")

    async def _insert() -> int:
        async with TestSession() as session:
            db_admin = Admin(username=username, hashed_password=await hash_password(password), role_id=1)
            session.add(db_admin)
            await session.commit()
            await session.refresh(db_admin)
            return db_admin.id

    return {"id": asyncio.run(_insert()), "username": username, "password": password}


def _stored(username: str) -> Admin:
    async def _load() -> Admin:
        async with TestSession() as session:
            return (await session.execute(select(Admin).where(Admin.username == username))).scalar_one()

    return asyncio.run(_load())


def _drop(username: str) -> None:
    async def _delete() -> None:
        async with TestSession() as session:
            db_admin = (await session.execute(select(Admin).where(Admin.username == username))).scalar_one_or_none()
            if db_admin is not None:
                await session.delete(db_admin)
                await session.commit()

    asyncio.run(_delete())


def _login(username: str, password: str) -> int:
    return client.post(
        "/api/admin/token", data={"username": username, "password": password, "grant_type": "password"}
    ).status_code


def _session(owner: dict) -> dict[str, str]:
    response = client.post(
        "/api/admin/token",
        data={"username": owner["username"], "password": owner["password"], "grant_type": "password"},
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    return auth_headers(response.json()["access_token"])


def _key(session: dict[str, str], *, inherit: bool) -> dict[str, str]:
    body = {"name": unique_name("selfedit_key"), "inherit_permissions": inherit}
    if not inherit:
        body["permissions"] = ADMIN_EDITOR
    created = client.post("/api/api_key", headers=session, json=body)
    assert created.status_code == status.HTTP_201_CREATED, created.text
    return {"X-Api-Key": created.json()["api_key"]}


def test_an_inheriting_api_key_cannot_change_its_owners_password():
    owner = _create_owner()
    try:
        key_headers = _key(_session(owner), inherit=True)
        new_password = strong_password("StolenKey")
        response = client.put(f"/api/admin/by-id/{owner['id']}", headers=key_headers, json={"password": new_password})
        assert response.status_code == status.HTTP_403_FORBIDDEN, response.text
        assert _login(owner["username"], owner["password"]) == status.HTTP_200_OK
        assert _login(owner["username"], new_password) != status.HTTP_200_OK
    finally:
        _drop(owner["username"])


def test_an_inheriting_api_key_cannot_change_its_owners_password_by_username():
    owner = _create_owner()
    try:
        key_headers = _key(_session(owner), inherit=True)
        response = client.put(
            f"/api/admin/{owner['username']}", headers=key_headers, json={"password": strong_password("StolenKey")}
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN, response.text
        assert _login(owner["username"], owner["password"]) == status.HTTP_200_OK
    finally:
        _drop(owner["username"])


def test_an_inheriting_api_key_cannot_change_its_owners_telegram_id():
    owner = _create_owner()
    try:
        key_headers = _key(_session(owner), inherit=True)
        response = client.put(f"/api/admin/by-id/{owner['id']}", headers=key_headers, json={"telegram_id": 987654321})
        assert response.status_code == status.HTTP_403_FORBIDDEN, response.text
        assert _stored(owner["username"]).telegram_id is None
    finally:
        _drop(owner["username"])


def test_a_custom_api_key_cannot_change_its_admins_password():
    owner = _create_owner()
    try:
        key_headers = _key(_session(owner), inherit=False)
        response = client.put(
            f"/api/admin/by-id/{owner['id']}", headers=key_headers, json={"password": strong_password("StolenKey")}
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN, response.text
        assert _login(owner["username"], owner["password"]) == status.HTTP_200_OK
    finally:
        _drop(owner["username"])


def test_an_api_key_may_still_edit_non_credential_fields_of_its_admin():
    owner = _create_owner()
    try:
        key_headers = _key(_session(owner), inherit=True)
        response = client.put(f"/api/admin/by-id/{owner['id']}", headers=key_headers, json={"note": "bot note"})
        assert response.status_code == status.HTTP_200_OK, response.text
        assert _stored(owner["username"]).note == "bot note"
    finally:
        _drop(owner["username"])


def test_an_api_key_may_resubmit_the_unchanged_telegram_id():
    owner = _create_owner()
    try:
        session = _session(owner)
        assert (
            client.put(f"/api/admin/by-id/{owner['id']}", headers=session, json={"telegram_id": 55500011}).status_code
            == status.HTTP_200_OK
        )
        key_headers = _key(session, inherit=True)
        response = client.put(
            f"/api/admin/by-id/{owner['id']}", headers=key_headers, json={"telegram_id": 55500011, "note": "same"}
        )
        assert response.status_code == status.HTTP_200_OK, response.text
    finally:
        _drop(owner["username"])


def test_a_session_admin_can_still_change_its_own_password_and_telegram_id():
    owner = _create_owner()
    try:
        new_password = strong_password("SessionChange")
        response = client.put(
            f"/api/admin/by-id/{owner['id']}",
            headers=_session(owner),
            json={"password": new_password, "telegram_id": 123123123},
        )
        assert response.status_code == status.HTTP_200_OK, response.text
        assert _login(owner["username"], new_password) == status.HTTP_200_OK
        assert _stored(owner["username"]).telegram_id == 123123123
    finally:
        _drop(owner["username"])


def test_concurrent_api_key_password_changes_never_land_beside_a_session_change():
    owner = _create_owner()
    try:
        session = _session(owner)
        key_headers = _key(session, inherit=True)
        session_password = strong_password("SessionRace")
        key_passwords = [strong_password(f"KeyRace{i}") for i in range(4)]

        url = f"/api/admin/by-id/{owner['id']}"
        calls = [("PUT", url, {"headers": key_headers, "json": {"password": password}}) for password in key_passwords]
        calls.append(("PUT", url, {"headers": session, "json": {"password": session_password}}))
        *key_codes, session_code = [response.status_code for response in send_concurrently(calls)]

        assert key_codes == [status.HTTP_403_FORBIDDEN] * len(key_passwords)
        assert session_code == status.HTTP_200_OK
        for password in key_passwords:
            assert _login(owner["username"], password) != status.HTTP_200_OK
    finally:
        _drop(owner["username"])
