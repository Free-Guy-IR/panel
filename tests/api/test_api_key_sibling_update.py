import asyncio

from fastapi import status
from sqlalchemy import select

from app.db.models import Admin
from app.models.admin import hash_password
from tests.api import TestSession, client
from tests.api.concurrent_requests import send_concurrently
from tests.api.helpers import auth_headers, strong_password, unique_name

KEY_EDITOR = {"api_keys": {"read": True, "update": True}}
BROAD = {"api_keys": {"read": True, "update": True}, "admins": {"read": True, "update": True}}


def _create_owner() -> dict:
    username = unique_name("sibling_owner")
    password = strong_password("SiblingOwner")

    async def _insert() -> int:
        async with TestSession() as session:
            db_admin = Admin(username=username, hashed_password=await hash_password(password), role_id=1)
            session.add(db_admin)
            await session.commit()
            await session.refresh(db_admin)
            return db_admin.id

    return {"id": asyncio.run(_insert()), "username": username, "password": password}


def _drop(username: str) -> None:
    async def _delete() -> None:
        async with TestSession() as session:
            db_admin = (await session.execute(select(Admin).where(Admin.username == username))).scalar_one_or_none()
            if db_admin is not None:
                await session.delete(db_admin)
                await session.commit()

    asyncio.run(_delete())


def _session(owner: dict) -> dict[str, str]:
    response = client.post(
        "/api/admin/token",
        data={"username": owner["username"], "password": owner["password"], "grant_type": "password"},
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    return auth_headers(response.json()["access_token"])


def _create_key(session: dict[str, str], permissions: dict | None, **extra) -> dict:
    body = {"name": unique_name("sibling_key"), "inherit_permissions": permissions is None, **extra}
    if permissions is not None:
        body["permissions"] = permissions
    created = client.post("/api/api_key", headers=session, json=body)
    assert created.status_code == status.HTTP_201_CREATED, created.text
    return created.json()


def test_a_narrow_key_cannot_re_enable_a_broader_sibling_key():
    owner = _create_owner()
    try:
        session = _session(owner)
        narrow = _create_key(session, KEY_EDITOR)
        broad = _create_key(session, BROAD)
        disabled = client.patch(f"/api/api_key/{broad['id']}", headers=session, json={"status": "disabled"})
        assert disabled.status_code == status.HTTP_200_OK, disabled.text

        response = client.patch(
            f"/api/api_key/{broad['id']}", headers={"X-Api-Key": narrow["api_key"]}, json={"status": "active"}
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN, response.text
        assert client.get(f"/api/api_key/{broad['id']}", headers=session).json()["status"] == "disabled"
    finally:
        _drop(owner["username"])


def test_a_narrow_key_cannot_extend_a_broader_sibling_key():
    owner = _create_owner()
    try:
        session = _session(owner)
        narrow = _create_key(session, KEY_EDITOR)
        broad = _create_key(session, BROAD, expire_date="2030-01-01T00:00:00Z")
        before = client.get(f"/api/api_key/{broad['id']}", headers=session).json()

        response = client.patch(
            f"/api/api_key/{broad['id']}",
            headers={"X-Api-Key": narrow["api_key"]},
            json={"expire_date": "2099-01-01T00:00:00Z"},
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN, response.text
        after = client.get(f"/api/api_key/{broad['id']}", headers=session).json()
        assert after["expire_date"] == before["expire_date"]
    finally:
        _drop(owner["username"])


def test_a_key_may_still_update_a_sibling_within_its_own_permissions():
    owner = _create_owner()
    try:
        session = _session(owner)
        editor = _create_key(session, KEY_EDITOR)
        sibling = _create_key(session, {"api_keys": {"read": True}})
        new_name = unique_name("within")

        response = client.patch(
            f"/api/api_key/{sibling['id']}", headers={"X-Api-Key": editor["api_key"]}, json={"name": new_name}
        )

        assert response.status_code == status.HTTP_200_OK, response.text
        assert response.json()["name"] == new_name
    finally:
        _drop(owner["username"])


def test_a_key_may_still_rename_itself():
    owner = _create_owner()
    try:
        session = _session(owner)
        editor = _create_key(session, KEY_EDITOR)
        new_name = unique_name("self_rename")

        response = client.patch(
            f"/api/api_key/{editor['id']}", headers={"X-Api-Key": editor["api_key"]}, json={"name": new_name}
        )

        assert response.status_code == status.HTTP_200_OK, response.text
    finally:
        _drop(owner["username"])


def test_an_inheriting_owner_key_like_the_bots_may_still_update_a_sibling():
    owner = _create_owner()
    try:
        session = _session(owner)
        bot_key = _create_key(session, None)
        broad = _create_key(session, BROAD)
        new_name = unique_name("bot_rename")

        response = client.patch(
            f"/api/api_key/{broad['id']}", headers={"X-Api-Key": bot_key["api_key"]}, json={"name": new_name}
        )

        assert response.status_code == status.HTTP_200_OK, response.text
    finally:
        _drop(owner["username"])


def test_concurrent_sibling_updates_by_a_narrow_key_all_fail_while_the_session_succeeds():
    owner = _create_owner()
    try:
        session = _session(owner)
        narrow = _create_key(session, KEY_EDITOR)
        broad = _create_key(session, BROAD)
        session_name = unique_name("session_wins")

        url = f"/api/api_key/{broad['id']}"
        narrow_names = [unique_name(f"race{i}") for i in range(4)]
        calls = [
            ("PATCH", url, {"headers": {"X-Api-Key": narrow["api_key"]}, "json": {"name": name}})
            for name in narrow_names
        ]
        calls.append(("PATCH", url, {"headers": session, "json": {"name": session_name}}))
        *narrow_codes, session_code = [response.status_code for response in send_concurrently(calls)]

        assert narrow_codes == [status.HTTP_403_FORBIDDEN] * 4
        assert session_code == status.HTTP_200_OK
        assert client.get(f"/api/api_key/{broad['id']}", headers=session).json()["name"] not in narrow_names
    finally:
        _drop(owner["username"])
