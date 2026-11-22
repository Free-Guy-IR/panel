from __future__ import annotations

import asyncio
from datetime import UTC, datetime as dt, timedelta as td

import pytest
from sqlalchemy import select, update
from sqlalchemy.sql.dml import Update

from app.db.models import User, UserStatus
from app.models.admin import AdminDetails, AdminRoleData
from app.models.user import BulkUser
from app.operation import OperatorType, user as user_operation
from app.operation.user import UserOperation
from tests.test_recorder_harness import file_backed_database_url, recorder_database, seed_users_and_nodes

OWNER = AdminDetails(id=None, username="owner", role=AdminRoleData(id=1, name="owner", is_owner=True))
STEP = -2 * 3600


@pytest.fixture(name="recorder_db")
async def recorder_db_fixture(monkeypatch: pytest.MonkeyPatch, tmp_path):
    async with recorder_database(monkeypatch, file_backed_database_url(tmp_path)) as session_factory:
        yield session_factory


@pytest.mark.asyncio
async def test_a_user_whose_expire_moves_during_a_bulk_expire_is_pushed_whenever_the_bulk_expires_it(
    recorder_db, monkeypatch
):
    _, (moved, plain), _ = await seed_users_and_nodes(recorder_db, user_count=2, node_count=0)
    now = dt.now(UTC)
    async with recorder_db() as session:
        await session.execute(
            update(User).where(User.id == moved).values(status=UserStatus.active, expire=now + td(hours=3))
        )
        await session.execute(
            update(User).where(User.id == plain).values(status=UserStatus.active, expire=now + td(hours=1))
        )
        await session.commit()

    pushed: list[int] = []

    async def capture_push(users) -> None:
        pushed.extend(user.id for user in users)

    monkeypatch.setattr(user_operation, "sync_users", capture_push)

    async def move_expire():
        async with recorder_db() as other:
            await other.execute(update(User).where(User.id == moved).values(expire=dt.now(UTC) + td(hours=1)))
            await other.commit()

    writers: list[asyncio.Task] = []
    async with recorder_db() as session:
        execute = session.execute

        async def execute_after_a_concurrent_writer(statement, *args, **kwargs):
            if not writers and isinstance(statement, Update) and statement.table.name == User.__tablename__:
                writers.append(asyncio.create_task(move_expire()))
                await asyncio.wait(writers, timeout=1.0)
            return await execute(statement, *args, **kwargs)

        session.execute = execute_after_a_concurrent_writer
        bulk = BulkUser(amount=STEP, users=[moved, plain])
        await UserOperation(operator_type=OperatorType.API).bulk_modify_expire(session, bulk, OWNER)
    assert writers, "the bulk expire never wrote"
    await asyncio.wait_for(writers[0], timeout=60)

    async with recorder_db() as session:
        rows = await session.execute(select(User.id, User.status).where(User.id.in_([moved, plain])))
        statuses = {user_id: user_status for user_id, user_status in rows.all()}
    assert statuses[plain] == UserStatus.expired, statuses
    expired = {user_id for user_id, user_status in statuses.items() if user_status == UserStatus.expired}
    assert expired <= set(pushed), (statuses, pushed)
