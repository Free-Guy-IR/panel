import asyncio

from fastapi import status
from sqlalchemy import select

from app.db.models import Admin
from app.models.admin import hash_password
from tests.api import TestSession, client
from tests.api.helpers import auth_headers, strong_password, unique_name

KEY_EDITOR = {"api_keys": {"read": True, "update": True}}


def _create_owner() -> dict:
    username = unique_name("inherit_owner")
    password = strong_password("InheritOwner")

    async def _insert() -> int:
        async with TestSession() as session:
            db_admin = Admin(username=username, hashed_password=await hash_password(password), role_id=1)
            session.add(db_admin)
            await session.commit()
            await session.refresh(db_admin)
            return db_admin.id

    return {"id": asyncio.run(_insert()), "username": username, "password": password}


def _drop_owner(username: str) -> None:
    async def _delete() -> None:
        async with TestSession() as session:
            db_admin = (await session.execute(select(Admin).where(Admin.username == username))).scalar_one_or_none()
            if db_admin is not None:
                await session.delete(db_admin)
                await session.commit()

    asyncio.run(_delete())


def _owner_session(owner: dict) -> dict[str, str]:
    response = client.post(
        "/api/admin/token",
        data={"username": owner["username"], "password": owner["password"], "grant_type": "password"},
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    return auth_headers(response.json()["access_token"])


def _role_create_status(headers: dict[str, str]) -> int:
    response = client.post(
        "/api/admin-role",
        headers=headers,
        json={"name": unique_name("escalated_role"), "permissions": {}, "limits": {}, "features": {}, "access": {}},
    )
    if response.status_code == status.HTTP_201_CREATED:
        client.delete(f"/api/admin-role/{response.json()['id']}", headers=headers)
    return response.status_code


def test_an_owner_custom_key_cannot_promote_itself_to_owner_level():
    owner = _create_owner()
    try:
        session = _owner_session(owner)
        created = client.post(
            "/api/api_key",
            headers=session,
            json={"name": unique_name("key_editor"), "inherit_permissions": False, "permissions": KEY_EDITOR},
        )
        assert created.status_code == status.HTTP_201_CREATED, created.text
        key = created.json()
        key_headers = {"X-Api-Key": key["api_key"]}
        assert _role_create_status(key_headers) == status.HTTP_403_FORBIDDEN

        promoted = client.patch(f"/api/api_key/{key['id']}", headers=key_headers, json={"inherit_permissions": True})

        assert promoted.status_code == status.HTTP_403_FORBIDDEN, promoted.text
        assert _role_create_status(key_headers) == status.HTTP_403_FORBIDDEN
        stored = client.get(f"/api/api_key/{key['id']}", headers=session).json()
        assert stored["inherit_permissions"] is False
    finally:
        _drop_owner(owner["username"])
