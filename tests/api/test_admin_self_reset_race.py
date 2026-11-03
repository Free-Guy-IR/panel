import asyncio

import pytest
from fastapi import HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.operation.admin as admin_operation_module
from app.db.models import Admin, AdminStatus
from app.models.admin import BulkAdminSelection
from app.operation import OperatorType
from app.operation.admin import AdminOperation
from app.operation.admin_sync import limit_exceeded_admins
from app.routers.authentication import get_admin
from tests.api import DATABASE_URL, IS_SQLITE, client
from tests.api.helpers import auth_headers, create_admin, delete_admin, unique_name

PAUSE_SECONDS = 1.5
GIB = 1024**3


def _sessions():
    connect_args = {"timeout": 30} if IS_SQLITE else {}
    engine = create_async_engine(DATABASE_URL, poolclass=NullPool, connect_args=connect_args)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def admins(access_token):
    response = client.post(
        "/api/admin-role",
        headers=auth_headers(access_token),
        json={
            "name": unique_name("role_self_reset_race"),
            "permissions": {"admins": {"read": True, "update": True, "reset_usage": True}},
            "limits": {},
            "features": {},
            "access": {},
        },
    )
    assert response.status_code == status.HTTP_201_CREATED, response.text
    role_id = response.json()["id"]
    me = create_admin(access_token, role_id=role_id)
    other = create_admin(access_token, role_id=role_id)
    token = client.post(
        "/api/admin/token",
        data={"username": me["username"], "password": me["password"], "grant_type": "password"},
    ).json()["access_token"]
    try:
        yield {"me": me["id"], "other": other["id"], "token": token}
    finally:
        delete_admin(access_token, me["username"])
        delete_admin(access_token, other["username"])
        client.delete(f"/api/admin-role/{role_id}", headers=auth_headers(access_token))


@pytest.mark.skipif(":memory:" in DATABASE_URL, reason="needs a database shared by several connections")
@pytest.mark.parametrize("ordering", ["competitors_first", "refused_first"])
def test_a_refused_self_reset_never_lifts_a_concurrent_limit_or_loses_the_owner_reset(
    access_token, admins, monkeypatch, ordering
):
    operation = AdminOperation(operator_type=OperatorType.API)
    real_reset = admin_operation_module.reset_admin_usage
    me_id, other_id = admins["me"], admins["other"]

    async def _scenario():
        engine, sessions = _sessions()
        refused_paused: set[str] = set()
        reached = asyncio.Event()
        competitors_done = asyncio.Event()
        refused_done = asyncio.Event()
        resets: list[tuple[str, int]] = []

        async def _recording_reset(db, db_admin):
            resets.append((asyncio.current_task().get_name(), db_admin.id))
            return await real_reset(db, db_admin=db_admin)

        monkeypatch.setattr(admin_operation_module, "reset_admin_usage", _recording_reset)

        async with sessions() as db:
            await db.execute(
                update(Admin)
                .where(Admin.id == me_id)
                .values(used_traffic=20 * GIB, data_limit=10 * GIB, status=AdminStatus.active)
            )
            await db.execute(
                update(Admin)
                .where(Admin.id == other_id)
                .values(used_traffic=5 * GIB, data_limit=None, status=AdminStatus.active)
            )
            await db.commit()
            myself = await get_admin(db, admins["token"])
            owner = await get_admin(db, access_token)
            await db.rollback()
        assert myself is not None and not myself.is_limited

        async def _refused(call):
            if ordering == "competitors_first":
                refused_paused.add(asyncio.current_task().get_name())
                if len(refused_paused) == 2:
                    reached.set()
                await asyncio.wait_for(competitors_done.wait(), timeout=PAUSE_SECONDS)
            async with sessions() as db:
                try:
                    await call(db)
                except HTTPException as exc:
                    return exc
            return None

        async def _refused_requests():
            results = await asyncio.gather(
                asyncio.create_task(
                    _refused(
                        lambda db: operation.bulk_reset_admins_usage(
                            db, BulkAdminSelection(ids={other_id, me_id}), myself
                        )
                    ),
                    name="refused-bulk",
                ),
                asyncio.create_task(
                    _refused(lambda db: operation.reset_admin_usage_by_id(db, me_id, myself)),
                    name="refused-single",
                ),
            )
            refused_done.set()
            return results

        async def _competitors():
            gate = reached if ordering == "competitors_first" else refused_done
            await asyncio.wait_for(gate.wait(), timeout=PAUSE_SECONDS)
            async with sessions() as db:
                await operation.reset_admin_usage_by_id(db, other_id, owner)
            async with sessions() as db:
                await limit_exceeded_admins(db)
            competitors_done.set()

        try:
            refused, _ = await asyncio.gather(
                asyncio.create_task(_refused_requests(), name="refused-requests"),
                asyncio.create_task(_competitors(), name="owner"),
            )
            async with sessions() as db:
                rows = {
                    row.id: (int(row.used_traffic or 0), row.status)
                    for row in (
                        await db.execute(
                            select(Admin.id, Admin.used_traffic, Admin.status).where(Admin.id.in_([me_id, other_id]))
                        )
                    ).all()
                }
        finally:
            await engine.dispose()
        return refused, resets, rows

    refused, resets, rows = asyncio.run(_scenario())

    assert rows[me_id] == (20 * GIB, AdminStatus.limited), rows
    assert rows[other_id] == (0, AdminStatus.active), rows
    assert [entry for entry in resets if entry[0].startswith("refused")] == [], resets
    assert ("owner", other_id) in resets, resets
    for result in refused:
        assert isinstance(result, HTTPException), result
        assert result.status_code == status.HTTP_403_FORBIDDEN, result.detail
