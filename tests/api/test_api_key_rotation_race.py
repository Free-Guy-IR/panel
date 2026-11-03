import asyncio

import pytest
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.operation.api_key as api_key_operation_module
from app.models.api_key import APIKeyUpdate
from app.operation import OperatorType
from app.operation.api_key import APIKeyOperation
from app.routers.authentication import get_admin, get_admin_from_api_key
from tests.api import DATABASE_URL, IS_SQLITE, client
from tests.api.helpers import auth_headers, create_admin, delete_admin, unique_name

SCOPE_OWN = {"scope": 1}
SCOPE_ALL = {"scope": 2}
ROLE = {
    "users": {"read": SCOPE_ALL, "read_simple": SCOPE_ALL},
    "api_keys": {"create": True, "read": True, "update": True, "delete": True},
}
NARROW = {"users": {"read": SCOPE_OWN}}
ROTATOR = {"users": {"read": SCOPE_OWN}, "api_keys": {"read": True, "delete": True}}
PAUSE_SECONDS = 1.5


def _login(username: str, password: str) -> str:
    response = client.post(
        "/api/admin/token", data={"username": username, "password": password, "grant_type": "password"}
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    return response.json()["access_token"]


def _sessions():
    connect_args = {"timeout": 30} if IS_SQLITE else {}
    engine = create_async_engine(DATABASE_URL, poolclass=NullPool, connect_args=connect_args)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def scoped_admin(access_token):
    role = client.post(
        "/api/admin-role",
        headers=auth_headers(access_token),
        json={"name": unique_name("rotation_role"), "permissions": ROLE, "limits": {}, "features": {}, "access": {}},
    )
    assert role.status_code == status.HTTP_201_CREATED, role.text
    role_id = role.json()["id"]
    admin = create_admin(access_token, role_id=role_id)
    try:
        yield _login(admin["username"], admin["password"])
    finally:
        delete_admin(access_token, admin["username"])
        client.delete(f"/api/admin-role/{role_id}", headers=auth_headers(access_token))


def _key(token: str, permissions: dict) -> tuple[int, str]:
    response = client.post(
        "/api/api_key",
        headers=auth_headers(token),
        json={"name": unique_name("rotation_key"), "inherit_permissions": False, "permissions": permissions},
    )
    assert response.status_code == status.HTTP_201_CREATED, response.text
    return response.json()["id"], response.json()["api_key"]


def _without_nulls(value):
    if isinstance(value, dict):
        return {key: _without_nulls(item) for key, item in value.items() if item is not None}
    return value


def _authenticates(raw_key: str) -> bool:
    return client.get("/api/users", headers={"X-Api-Key": raw_key}).status_code == status.HTTP_200_OK


@pytest.mark.skipif(":memory:" in DATABASE_URL, reason="needs a database shared by several connections")
@pytest.mark.parametrize(
    "change",
    [{"permissions": {"users": {"read": SCOPE_ALL}}}, {"inherit_permissions": True}],
    ids=["widened", "inheriting"],
)
def test_a_custom_key_rotation_is_decided_on_the_row_it_rotates(access_token, scoped_admin, monkeypatch, change):
    _, rotator_raw = _key(scoped_admin, ROTATOR)
    target_id, target_raw = _key(scoped_admin, NARROW)
    operation = APIKeyOperation(operator_type=OperatorType.API)
    rotate = api_key_operation_module.revoke_api_key_crud

    async def _scenario():
        engine, sessions = _sessions()
        checked = asyncio.Event()
        changed = asyncio.Event()
        order: list[str] = []

        async def _rotate_after_owner_change(db, db_key):
            checked.set()
            try:
                await asyncio.wait_for(changed.wait(), timeout=PAUSE_SECONDS)
            except TimeoutError:
                pass
            order.append("rotate")
            return await rotate(db, db_key)

        monkeypatch.setattr(api_key_operation_module, "revoke_api_key_crud", _rotate_after_owner_change)

        async with sessions() as db:
            rotator = await get_admin_from_api_key(db, rotator_raw)
            owner = await get_admin(db, access_token)
            await db.rollback()

        async def _revoke():
            async with sessions() as db:
                try:
                    return await operation.revoke_api_key(db, admin=rotator, key_id=target_id)
                except HTTPException as exc:
                    return exc

        async def _owner_change():
            await checked.wait()
            async with sessions() as db:
                await operation.modify_api_key(
                    db, admin=owner, key_id=target_id, model=APIKeyUpdate.model_validate(change)
                )
            order.append("change")
            changed.set()

        try:
            revoked, _ = await asyncio.gather(_revoke(), _owner_change())
        finally:
            await engine.dispose()
        return revoked, order

    revoked, order = asyncio.run(_scenario())

    if order == ["change", "rotate"]:
        assert isinstance(revoked, HTTPException), revoked.permissions.model_dump(exclude_none=True)
        assert revoked.status_code == status.HTTP_403_FORBIDDEN
        assert _authenticates(target_raw)
    else:
        assert order == ["rotate", "change"]
        assert not isinstance(revoked, HTTPException), revoked.detail
        assert revoked.inherit_permissions is False
        assert revoked.permissions.model_dump(exclude_none=True) == NARROW
        assert not _authenticates(target_raw)
    stored = client.get(f"/api/api_key/{target_id}", headers=auth_headers(access_token)).json()
    for field, value in change.items():
        assert _without_nulls(stored[field]) == value
    assert _authenticates(rotator_raw)
