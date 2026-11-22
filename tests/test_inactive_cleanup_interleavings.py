from __future__ import annotations

from datetime import UTC, datetime as dt, timedelta as td

import pytest
from sqlalchemy import select, update
from sqlalchemy.sql import Select

from app.db.models import User, UserStatus
from app.jobs import record_usages
from app.models.admin import AdminDetails, AdminRoleData
from app.models.user import ExpiredUsersQuery
from app.operation import OperatorType, user as user_operation
from app.operation.user import UserOperation
from tests.test_recorder_harness import file_backed_database_url, recorder_database, seed_users_and_nodes

OWNER = AdminDetails(id=None, username="owner", role=AdminRoleData(id=1, name="owner", is_owner=True))
CUTOFF_DAYS = 30


@pytest.fixture(name="recorder_db")
async def recorder_db_fixture(monkeypatch: pytest.MonkeyPatch, tmp_path):
    async with recorder_database(monkeypatch, file_backed_database_url(tmp_path)) as session_factory:
        yield session_factory


async def _seed_idle_users(recorder_db, statuses: list[UserStatus]) -> list[int]:
    _, user_ids, _ = await seed_users_and_nodes(recorder_db, user_count=len(statuses), node_count=0)
    now = dt.now(UTC)
    async with recorder_db() as session:
        for user_id, user_status in zip(user_ids, statuses):
            await session.execute(
                update(User)
                .where(User.id == user_id)
                .values(
                    status=user_status,
                    created_at=now - td(days=90),
                    online_at=now - td(days=60),
                    edit_at=None,
                    last_status_change=now - td(days=60),
                )
            )
        await session.commit()
    return user_ids


async def _existing(recorder_db, user_ids: list[int]) -> dict[int, tuple]:
    async with recorder_db() as session:
        rows = await session.execute(select(User.id, User.status, User.online_at).where(User.id.in_(user_ids)))
        return {user_id: (user_status, online_at) for user_id, user_status, online_at in rows.all()}


async def _cleanup_with_a_write_after_its_target_read(recorder_db, monkeypatch, concurrent_write):
    removed_from_nodes: list[int] = []

    async def capture_node_removal(rows) -> None:
        removed_from_nodes.extend(row.id for row in rows)

    monkeypatch.setattr(user_operation, "sync_remove_users", capture_node_removal)
    fired = []
    async with recorder_db() as session:
        execute = session.execute

        async def execute_then_interleave(statement, *args, **kwargs):
            result = await execute(statement, *args, **kwargs)
            if not fired and isinstance(statement, Select) and User.__table__ in statement.get_final_froms():
                fired.append(statement)
                await concurrent_write()
            return result

        session.execute = execute_then_interleave
        query = ExpiredUsersQuery(target="inactive", expired_before=dt.now(UTC) - td(days=CUTOFF_DAYS))
        response = await UserOperation(operator_type=OperatorType.API).delete_expired_users(session, OWNER, query)
    assert fired, "the cleanup never read its targets"
    return response, removed_from_nodes


@pytest.mark.asyncio
async def test_a_user_the_recorder_marks_online_during_the_inactive_cleanup_is_not_deleted(recorder_db, monkeypatch):
    came_online, idle = await _seed_idle_users(recorder_db, [UserStatus.active, UserStatus.active])

    async def record_traffic():
        await record_usages.touch_users_online_at([came_online])

    response, removed_from_nodes = await _cleanup_with_a_write_after_its_target_read(
        recorder_db, monkeypatch, record_traffic
    )

    rows = await _existing(recorder_db, [came_online, idle])
    assert set(rows) == {came_online}, rows
    online_at = rows[came_online][1]
    assert online_at.replace(tzinfo=UTC) > dt.now(UTC) - td(minutes=5), online_at
    assert removed_from_nodes == [idle], removed_from_nodes
    assert response.count == 1, response
    assert len(response.users) == 1, response


@pytest.mark.asyncio
async def test_a_user_enabled_during_the_inactive_cleanup_is_removed_from_the_nodes_when_it_is_deleted(
    recorder_db, monkeypatch
):
    enabled, disabled = await _seed_idle_users(recorder_db, [UserStatus.disabled, UserStatus.disabled])

    async def enable_user():
        async with recorder_db() as other:
            await other.execute(
                update(User).where(User.id == enabled).values(status=UserStatus.active, last_status_change=dt.now(UTC))
            )
            await other.commit()

    response, removed_from_nodes = await _cleanup_with_a_write_after_its_target_read(
        recorder_db, monkeypatch, enable_user
    )

    assert await _existing(recorder_db, [enabled, disabled]) == {}
    assert enabled in removed_from_nodes, removed_from_nodes
    assert response.count == 2, response
