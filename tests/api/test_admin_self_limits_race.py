import asyncio

import pytest
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.operation.admin as admin_operation_module
from app.db.models import Admin
from app.models.admin import AdminModify
from app.operation import OperatorType
from app.operation.admin import AdminOperation
from app.routers.authentication import get_admin
from tests.api import DATABASE_URL, IS_SQLITE, client
from tests.api.helpers import auth_headers, create_admin, delete_admin, unique_name

PAUSE_SECONDS = 1.5
GIB = 1024**3
DASHBOARD_EMPTY_OVERRIDES = {
    "max_users": None,
    "data_limit_min": None,
    "data_limit_max": None,
    "expire_min": None,
    "expire_max": None,
    "min_hwid_per_user": None,
    "max_hwid_per_user": None,
    "on_hold_timeout_min": None,
    "on_hold_timeout_max": None,
}


def _sessions():
    connect_args = {"timeout": 30} if IS_SQLITE else {}
    engine = create_async_engine(DATABASE_URL, poolclass=NullPool, connect_args=connect_args)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _role(access_token: str) -> int:
    response = client.post(
        "/api/admin-role",
        headers=auth_headers(access_token),
        json={
            "name": unique_name("role_self_limits_race"),
            "permissions": {"admins": {"read": True, "update": True}},
            "limits": {"max_users": 5},
            "features": {},
            "access": {},
        },
    )
    assert response.status_code == status.HTTP_201_CREATED, response.text
    return response.json()["id"]


@pytest.fixture
def limited_admin(access_token):
    role_id = _role(access_token)
    stricter_role_id = _role(access_token)
    admin = create_admin(access_token, role_id=role_id)
    token = client.post(
        "/api/admin/token",
        data={"username": admin["username"], "password": admin["password"], "grant_type": "password"},
    ).json()["access_token"]
    try:
        yield {"id": admin["id"], "token": token, "role_id": role_id, "stricter_role_id": stricter_role_id}
    finally:
        delete_admin(access_token, admin["username"])
        client.delete(f"/api/admin-role/{role_id}", headers=auth_headers(access_token))
        client.delete(f"/api/admin-role/{stricter_role_id}", headers=auth_headers(access_token))


def _owner_change(case: str, admin: dict) -> dict:
    if case == "data_limit":
        return {"data_limit": GIB}
    if case == "overrides":
        return {"permission_overrides": {"max_users": 3}}
    return {"role_id": admin["stricter_role_id"]}


def _owner_change_survived(case: str, admin: dict, row: Admin) -> bool:
    if case == "data_limit":
        return row.data_limit == GIB
    if case == "overrides":
        return (row.permission_overrides or {}).get("max_users") == 3
    return row.role_id == admin["stricter_role_id"]


@pytest.mark.skipif(":memory:" in DATABASE_URL, reason="needs a database shared by several connections")
@pytest.mark.parametrize("case", ["data_limit", "overrides", "role"])
def test_a_stale_self_save_never_undoes_a_concurrent_owner_limit_change(access_token, limited_admin, monkeypatch, case):
    operation = AdminOperation(operator_type=OperatorType.API)
    real_transition_lock = admin_operation_module.admin_transition_lock
    self_save = AdminModify.model_validate(
        {
            "note": "edited by myself",
            "data_limit": 0,
            "role_id": limited_admin["role_id"],
            "permission_overrides": DASHBOARD_EMPTY_OVERRIDES,
        }
    )
    owner_change = AdminModify.model_validate(_owner_change(case, limited_admin))

    async def _scenario():
        engine, sessions = _sessions()
        checked = asyncio.Event()
        changed = asyncio.Event()
        order: list[str] = []
        paused = False

        class _PausedTransitionLock:
            def __init__(self):
                self.lock = real_transition_lock()

            async def __aenter__(self):
                nonlocal paused
                if not paused:
                    paused = True
                    checked.set()
                    try:
                        await asyncio.wait_for(changed.wait(), timeout=PAUSE_SECONDS)
                    except TimeoutError:
                        pass
                    order.append("self")
                return await self.lock.__aenter__()

            async def __aexit__(self, *exc_info):
                return await self.lock.__aexit__(*exc_info)

        monkeypatch.setattr(admin_operation_module, "admin_transition_lock", _PausedTransitionLock)

        async with sessions() as db:
            myself = await get_admin(db, limited_admin["token"])
            owner = await get_admin(db, access_token)
            await db.rollback()

        async def _self_save():
            async with sessions() as db:
                try:
                    return await operation.modify_admin_by_id(db, limited_admin["id"], self_save, myself)
                except HTTPException as exc:
                    return exc

        async def _owner():
            await checked.wait()
            async with sessions() as db:
                await operation.modify_admin_by_id(db, limited_admin["id"], owner_change, owner)
            order.append("change")
            changed.set()

        try:
            saved, _ = await asyncio.gather(_self_save(), _owner())
            async with sessions() as db:
                row = (await db.execute(select(Admin).where(Admin.id == limited_admin["id"]))).scalar_one()
        finally:
            await engine.dispose()
        return saved, order, row

    saved, order, row = asyncio.run(_scenario())

    assert order == ["change", "self"], order
    assert isinstance(saved, HTTPException), saved
    assert saved.status_code == status.HTTP_403_FORBIDDEN, saved.detail
    assert _owner_change_survived(case, limited_admin, row), (row.data_limit, row.permission_overrides, row.role_id)
    assert row.note != "edited by myself"
