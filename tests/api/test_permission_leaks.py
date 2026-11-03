"""Regression tests for RBAC permission leaks."""

import asyncio

import pytest
from fastapi import status
from sqlalchemy import select

from app.db.models import Admin
from app.models.admin import hash_password
from tests.api import TestSession, client
from tests.api.helpers import (
    auth_headers,
    create_admin,
    create_user,
    delete_admin,
    strong_password,
    unique_name,
)

SCOPE_OWN = {"scope": 1}
SCOPE_ALL = {"scope": 2}


def _login(username: str, password: str) -> str:
    response = client.post(
        "/api/admin/token",
        data={"username": username, "password": password, "grant_type": "password"},
    )
    assert response.status_code == status.HTTP_200_OK
    return response.json()["access_token"]


def _create_role(access_token: str, permissions: dict, access: dict | None = None) -> dict:
    response = client.post(
        "/api/admin-role",
        headers=auth_headers(access_token),
        json={
            "name": unique_name("role_leak"),
            "permissions": permissions,
            "limits": {},
            "features": {},
            "access": access or {},
        },
    )
    assert response.status_code == status.HTTP_201_CREATED, response.text
    return response.json()


def _delete_role(access_token: str, role_id: int) -> None:
    client.delete(f"/api/admin-role/{role_id}", headers=auth_headers(access_token))


def _create_owner_admin() -> dict:
    username = unique_name("owner_key")
    password = strong_password("OwnerKey")

    async def _create() -> int:
        async with TestSession() as session:
            db_admin = Admin(username=username, hashed_password=await hash_password(password), role_id=1)
            session.add(db_admin)
            await session.commit()
            await session.refresh(db_admin)
            return db_admin.id

    return {"id": asyncio.run(_create()), "username": username, "password": password}


def _delete_admin_row(username: str) -> None:
    async def _delete() -> None:
        async with TestSession() as session:
            db_admin = (await session.execute(select(Admin).where(Admin.username == username))).scalar_one_or_none()
            if db_admin is not None:
                await session.delete(db_admin)
                await session.commit()

    asyncio.run(_delete())


def _create_api_key(token: str, **payload) -> str:
    response = client.post(
        "/api/api_key",
        headers=auth_headers(token),
        json={"name": unique_name("api_key"), **payload},
    )
    assert response.status_code == status.HTTP_201_CREATED, response.text
    return response.json()["api_key"]


# --- API keys with custom (non-inherited) permissions ---


def test_api_key_with_empty_custom_permissions_grants_nothing(access_token):
    admin = create_admin(access_token, role_id=2)
    try:
        raw_key = _create_api_key(
            _login(admin["username"], admin["password"]),
            inherit_permissions=False,
            permissions={},
        )

        response = client.get("/api/users", headers={"X-Api-Key": raw_key})

        assert response.status_code == status.HTTP_403_FORBIDDEN
    finally:
        delete_admin(access_token, admin["username"])


def test_owner_api_key_with_empty_custom_permissions_is_not_owner(access_token):
    owner = _create_owner_admin()
    try:
        raw_key = _create_api_key(
            _login(owner["username"], owner["password"]),
            inherit_permissions=False,
            permissions={},
        )

        response = client.get("/api/admin-roles", headers={"X-Api-Key": raw_key})

        assert response.status_code == status.HTTP_403_FORBIDDEN
    finally:
        _delete_admin_row(owner["username"])


@pytest.fixture
def scoped_actor(access_token):
    """Build a non-owner admin with the given permissions, plus a user that admin does not own."""
    created = []

    def _build(permissions: dict) -> dict:
        role = _create_role(access_token, permissions)
        actor = create_admin(access_token, role_id=role["id"])
        victim = create_user(access_token)
        created.append((role, actor, victim))
        return {"actor": actor, "token": _login(actor["username"], actor["password"]), "victim": victim}

    yield _build

    for role, actor, victim in created:
        client.delete(f"/api/user/by-id/{victim['id']}", headers=auth_headers(access_token))
        delete_admin(access_token, actor["username"])
        _delete_role(access_token, role["id"])


def _api_key_actor(scoped_actor):
    return scoped_actor(
        {
            "users": {"read": SCOPE_OWN, "read_simple": SCOPE_OWN},
            "api_keys": {"create": True, "read": True, "update": True},
        }
    )


def test_api_key_patch_cannot_carry_permissions_beyond_the_admin(scoped_actor):
    context = _api_key_actor(scoped_actor)
    headers = auth_headers(context["token"])
    created = client.post("/api/api_key", headers=headers, json={"name": unique_name("api_key")})
    assert created.status_code == status.HTTP_201_CREATED, created.text
    key_id, raw_key = created.json()["id"], created.json()["api_key"]

    # Permissions sent while the key still inherits must not be stored...
    client.patch(
        f"/api/api_key/{key_id}", headers=headers, json={"permissions": {"users": {"read": True, "read_simple": True}}}
    )
    stored = client.get(f"/api/api_key/{key_id}", headers=headers).json()["permissions"]
    assert all(value is None for value in stored.values()), stored

    # ...so switching inheritance off afterwards leaves a key with no permissions at all.
    client.patch(f"/api/api_key/{key_id}", headers=headers, json={"inherit_permissions": False})
    response = client.get(
        "/api/users", headers={"X-Api-Key": raw_key}, params={"username": context["victim"]["username"]}
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN


def test_api_key_patch_with_null_permissions_keeps_the_key_working(scoped_actor):
    context = _api_key_actor(scoped_actor)
    headers = auth_headers(context["token"])
    created = client.post(
        "/api/api_key",
        headers=headers,
        json={
            "name": unique_name("api_key"),
            "inherit_permissions": False,
            "permissions": {"users": {"read": SCOPE_OWN}},
        },
    )
    assert created.status_code == status.HTTP_201_CREATED, created.text
    key_id, raw_key = created.json()["id"], created.json()["api_key"]

    renamed = client.patch(
        f"/api/api_key/{key_id}", headers=headers, json={"name": unique_name("renamed"), "permissions": None}
    )
    assert renamed.status_code == status.HTTP_200_OK, renamed.text

    assert client.get("/api/admin", headers={"X-Api-Key": raw_key}).status_code == status.HTTP_200_OK
