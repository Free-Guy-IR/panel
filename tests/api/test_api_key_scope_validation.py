import asyncio

import pytest
from fastapi import status
from sqlalchemy import select

from app.db.models import Admin
from app.models.admin import AdminDetails, AdminRoleData, hash_password
from app.models.admin_role import RolePermissions
from app.operation.api_key import _check_permissions_not_exceed_admin
from tests.api import TestSession, client
from tests.api.helpers import auth_headers, strong_password, unique_name


def _create_owner_admin() -> dict:
    username = unique_name("scope_owner")
    password = strong_password("ScopeOwner")

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


def _admin_with(permissions: dict) -> AdminDetails:
    return AdminDetails(
        id=42,
        username="scoped",
        role=AdminRoleData(name="scoped", permissions=RolePermissions.model_validate(permissions), is_owner=False),
    )


def test_a_key_cannot_scope_an_action_its_admin_is_explicitly_denied():
    admin = _admin_with({"users": {"read": {"scope": 2}, "delete": False}})
    with pytest.raises(ValueError):
        _check_permissions_not_exceed_admin(admin, RolePermissions.model_validate({"users": {"delete": {"scope": 1}}}))


def test_a_key_within_its_admin_scope_is_allowed():
    admin = _admin_with({"users": {"read": {"scope": 2}, "delete": {"scope": 1}}})
    _check_permissions_not_exceed_admin(admin, RolePermissions.model_validate({"users": {"delete": {"scope": 1}}}))


@pytest.mark.parametrize(
    "scoped",
    [{"scope": 99}, {"scope": -1}, {"scope": 1, "bogus": 2}],
)
def test_owner_created_keys_still_reject_malformed_scopes(scoped):
    owner = _create_owner_admin()
    try:
        token = client.post(
            "/api/admin/token",
            data={"username": owner["username"], "password": owner["password"], "grant_type": "password"},
        ).json()["access_token"]
        response = client.post(
            "/api/api_key",
            headers=auth_headers(token),
            json={
                "name": unique_name("bad_scope"),
                "inherit_permissions": False,
                "permissions": {"users": {"read": scoped}},
            },
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN, response.text
    finally:
        _delete_admin_row(owner["username"])
