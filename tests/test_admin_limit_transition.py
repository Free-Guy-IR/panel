from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from app.db.crud.admin import get_admin_by_id
from app.db.models import Admin, AdminStatus
from app.jobs import record_usages
from app.models.admin import AdminDetails, AdminModify, AdminRoleData
from app.operation import OperatorType, admin as admin_operation, admin_sync
from tests.test_recorder_harness import recorder_db_fixture, seed_users_and_nodes  # noqa: F401

OWNER = AdminDetails(id=None, username="owner", role=AdminRoleData(id=1, name="owner", is_owner=True))


@pytest.fixture(name="node_pushes")
def node_pushes_fixture(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, list[int]]]:
    pushes: list[tuple[str, list[int]]] = []

    async def record_remove(users):
        pushes.append(("remove", sorted(user.id for user in users)))

    async def record_sync(users):
        pushes.append(("sync", sorted(user.id for user in users)))

    monkeypatch.setattr(admin_sync, "sync_remove_users", record_remove)
    monkeypatch.setattr(admin_sync, "sync_users", record_sync)
    monkeypatch.setattr(admin_operation.notification, "admin_usage_reset", AsyncMock())
    monkeypatch.setattr(admin_operation.notification, "modify_admin", AsyncMock())
    return pushes


async def _over_quota_admin(recorder_db) -> tuple[int, list[int]]:
    admin_id, user_ids, _ = await seed_users_and_nodes(recorder_db, user_count=2, node_count=0)
    async with recorder_db() as session:
        await session.execute(
            update(Admin)
            .where(Admin.id == admin_id)
            .values(status=AdminStatus.active, data_limit=1000, used_traffic=1500)
        )
        await session.commit()
    return admin_id, user_ids


async def _admin_row(recorder_db, admin_id: int):
    async with recorder_db() as session:
        return (
            await session.execute(
                select(Admin.status, Admin.data_limit, Admin.used_traffic).where(Admin.id == admin_id)
            )
        ).one()


@pytest.mark.asyncio
async def test_admin_limit_job_does_not_limit_an_admin_whose_quota_was_raised_after_its_read(
    monkeypatch: pytest.MonkeyPatch, recorder_db
):
    first_admin_id, _, _ = await seed_users_and_nodes(recorder_db, user_count=1, node_count=0)
    second_admin_id, _, _ = await seed_users_and_nodes(recorder_db, user_count=1, node_count=0)
    async with recorder_db() as session:
        await session.execute(
            update(Admin)
            .where(Admin.id.in_([first_admin_id, second_admin_id]))
            .values(status=AdminStatus.active, data_limit=1000, used_traffic=1500)
        )
        await session.commit()

    calls = {"n": 0}

    async def sync_then_owner_raises_second_quota(db, admin, was_blocked):
        calls["n"] += 1
        if calls["n"] == 1:
            async with recorder_db() as owner_session:
                await owner_session.execute(update(Admin).where(Admin.id == second_admin_id).values(data_limit=10_000))
                await owner_session.commit()
        return 0

    monkeypatch.setattr(admin_sync, "sync_admin_users_for_block_transition", sync_then_owner_raises_second_quota)

    async with record_usages.GetDB() as db:
        await admin_sync.limit_exceeded_admins(db)

    async with recorder_db() as session:
        row = (
            await session.execute(
                select(Admin.status, Admin.data_limit, Admin.used_traffic).where(Admin.id == second_admin_id)
            )
        ).one()
    assert (row.data_limit, row.used_traffic) == (10_000, 1500)
    assert row.status == AdminStatus.active, row


@pytest.mark.asyncio
async def test_a_reset_reactivates_an_admin_the_job_limited_after_the_reset_read_it(recorder_db, node_pushes):
    admin_id, user_ids = await _over_quota_admin(recorder_db)

    async with recorder_db() as reset_session:
        db_admin = await get_admin_by_id(reset_session, admin_id)
        assert db_admin.status == AdminStatus.active

        async with record_usages.GetDB() as job_session:
            assert await admin_sync.limit_exceeded_admins(job_session) == 1

        await admin_operation.AdminOperation(OperatorType.API)._reset_admin_usage(reset_session, db_admin, OWNER)

    row = await _admin_row(recorder_db, admin_id)
    assert (row.status, row.used_traffic) == (AdminStatus.active, 0), row
    assert node_pushes == [("remove", user_ids), ("sync", user_ids)], node_pushes


@pytest.mark.asyncio
async def test_a_quota_raise_reactivates_an_admin_the_job_limited_after_the_modify_read_it(recorder_db, node_pushes):
    admin_id, user_ids = await _over_quota_admin(recorder_db)

    async with recorder_db() as modify_session:
        db_admin = await get_admin_by_id(modify_session, admin_id)
        assert db_admin.status == AdminStatus.active

        async with record_usages.GetDB() as job_session:
            assert await admin_sync.limit_exceeded_admins(job_session) == 1

        await admin_operation.AdminOperation(OperatorType.API)._modify_admin(
            modify_session, db_admin, AdminModify(data_limit=10_000), OWNER
        )

    row = await _admin_row(recorder_db, admin_id)
    assert (row.status, row.data_limit, row.used_traffic) == (AdminStatus.active, 10_000, 1500), row
    assert node_pushes == [("remove", user_ids), ("sync", user_ids)], node_pushes


@pytest.mark.asyncio
async def test_a_reset_waits_for_the_limit_job_to_finish_removing_users_before_it_resyncs_them(
    monkeypatch: pytest.MonkeyPatch, recorder_db, node_pushes
):
    admin_id, user_ids = await _over_quota_admin(recorder_db)
    removal_started = asyncio.Event()
    release_removal = asyncio.Event()

    async def slow_remove(users):
        removal_started.set()
        await release_removal.wait()
        node_pushes.append(("remove", sorted(user.id for user in users)))

    monkeypatch.setattr(admin_sync, "sync_remove_users", slow_remove)

    async def run_job():
        async with record_usages.GetDB() as job_session:
            return await admin_sync.limit_exceeded_admins(job_session)

    async def run_reset():
        async with recorder_db() as reset_session:
            db_admin = await get_admin_by_id(reset_session, admin_id)
            await admin_operation.AdminOperation(OperatorType.API)._reset_admin_usage(reset_session, db_admin, OWNER)

    def reset_is_parked_or_has_pushed() -> bool:
        transition_lock = getattr(admin_sync, "admin_transition_lock", None)
        parked = transition_lock is not None and bool(transition_lock()._waiters)
        return parked or any(kind == "sync" for kind, _ in node_pushes)

    job = asyncio.create_task(run_job())
    await removal_started.wait()
    reset = asyncio.create_task(run_reset())
    for _ in range(500):
        if reset_is_parked_or_has_pushed():
            break
        await asyncio.sleep(0.01)
    assert reset_is_parked_or_has_pushed()
    release_removal.set()
    assert await job == 1
    await reset

    row = await _admin_row(recorder_db, admin_id)
    assert (row.status, row.used_traffic) == (AdminStatus.active, 0), row
    assert node_pushes == [("remove", user_ids), ("sync", user_ids)], node_pushes


@pytest.mark.asyncio
async def test_the_limit_job_leaves_an_admin_alone_once_a_reset_committed_first(recorder_db, node_pushes):
    admin_id, _ = await _over_quota_admin(recorder_db)

    async with recorder_db() as reset_session:
        db_admin = await get_admin_by_id(reset_session, admin_id)
        await admin_operation.AdminOperation(OperatorType.API)._reset_admin_usage(reset_session, db_admin, OWNER)

    async with record_usages.GetDB() as job_session:
        assert await admin_sync.limit_exceeded_admins(job_session) == 0

    row = await _admin_row(recorder_db, admin_id)
    assert (row.status, row.used_traffic) == (AdminStatus.active, 0), row
    assert node_pushes == [], node_pushes
