from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import Column, event, select, update
from sqlalchemy.orm import Session
from sqlalchemy.sql import visitors
from sqlalchemy.sql.util import find_tables

import app.settings as settings_module
from app import notification
from app.db.crud.admin import get_admin_by_id
from app.db.crud.user import get_user_by_id
from app.db.models import User, UserStatus
from app.fork.jobs import connection_limiter
from app.fork.models.connection import ConnectionRestriction
from app.models.admin import AdminDetails, AdminRoleData
from app.models.settings import ConnectionLimit
from app.models.user import UserStatusToggle
from app.operation import OperatorType, admin as admin_operation, user as user_operation
from app.operation.admin import AdminOperation
from app.operation.user import UserOperation
from tests.test_recorder_harness import _database_url, recorder_db_fixture, seed_users_and_nodes  # noqa: F401
from tests.test_status_lock_order_interleavings import _deadlocks

OWNER = AdminDetails(id=None, username="owner", role=AdminRoleData(id=1, name="owner", is_owner=True))
SETTINGS = ConnectionLimit(
    enabled=True, monitor_only=False, enforcement_enabled=True, persistence_cycles=1, punishment_steps=[30]
)
ADMIN_WIDE = {
    "activate_all": "_activate_all_disabled_users_for_admin",
    "disable_all": "_disable_all_active_users_for_admin",
}
USERS = 6
HOLD_SECONDS = 1.0
server_database_only = pytest.mark.skipif(
    _database_url().startswith("sqlite"), reason="needs a server database with row locks and deadlock detection"
)


@pytest.fixture(autouse=True)
async def quiet(monkeypatch: pytest.MonkeyPatch):
    stored = SimpleNamespace(
        telegram={}, webhook={}, notification_settings={}, notification_enable={}, subscription={}, hwid={}, general={}
    )
    monkeypatch.setattr(settings_module, "stored_settings", AsyncMock(return_value=stored))
    await settings_module.refresh_caches()
    monkeypatch.setattr(UserOperation, "generate_subscription_url", staticmethod(AsyncMock(return_value="")))
    for name in ("user_status_change", "modify_user", "reset_user_data_usage"):
        monkeypatch.setattr(notification, name, AsyncMock())
    monkeypatch.setattr(admin_operation, "sync_users", AsyncMock())
    monkeypatch.setattr(user_operation, "sync_user", AsyncMock())
    monkeypatch.setattr(connection_limiter, "_push_to_nodes", AsyncMock())
    yield
    await settings_module.refresh_caches()


@pytest.fixture(name="user_targets")
def user_targets_fixture():
    seen: list[tuple[str, set[str]]] = []

    def on_orm(state):
        statement = state.statement
        if state.is_select and statement._for_update_arg is not None:
            kind = "lock"
            tables = {table.name for from_ in statement.get_final_froms() for table in find_tables(from_)}
        elif state.is_update:
            kind = "write"
            tables = {statement.table.name}
        else:
            return
        if tables != {"users"} or statement.whereclause is None:
            return
        seen.append(
            (
                kind,
                {
                    element.key
                    for element in visitors.iterate(statement.whereclause)
                    if isinstance(element, Column) and element.table.name == "users"
                },
            )
        )

    event.listen(Session, "do_orm_execute", on_orm)
    yield seen
    event.remove(Session, "do_orm_execute", on_orm)


async def _seed(recorder_db, status: UserStatus) -> tuple[int, list[int]]:
    admin_id, user_ids, _ = await seed_users_and_nodes(recorder_db, user_count=USERS, node_count=0)
    async with recorder_db() as session:
        await session.execute(
            update(User)
            .where(User.id.in_(user_ids))
            .values(status=status, last_status_change=datetime.now(UTC) - timedelta(minutes=40))
        )
        if status == UserStatus.disabled:
            session.add_all(
                ConnectionRestriction(
                    user_id=user_id,
                    created_at=datetime.now(UTC) - timedelta(minutes=30),
                    previous_status=UserStatus.active.value,
                    disable_minutes=30,
                    restore_at=datetime.now(UTC) + timedelta(minutes=30),
                    active=True,
                )
                for user_id in user_ids
            )
        await session.commit()
    return admin_id, user_ids


async def _admin_wide(recorder_db, admin_id: int, action: str) -> None:
    async with recorder_db() as db:
        db_admin = await get_admin_by_id(db, admin_id)
        await getattr(AdminOperation(OperatorType.API), ADMIN_WIDE[action])(db, db_admin, OWNER)


async def _final(recorder_db, user_ids) -> list[tuple[UserStatus, int]]:
    async with recorder_db() as session:
        statuses = dict((await session.execute(select(User.id, User.status).where(User.id.in_(user_ids)))).all())
        active = (
            (
                await session.execute(
                    select(ConnectionRestriction.user_id).where(
                        ConnectionRestriction.user_id.in_(user_ids), ConnectionRestriction.active.is_(True)
                    )
                )
            )
            .scalars()
            .all()
        )
    return [(statuses[user_id], active.count(user_id)) for user_id in user_ids]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", list(ADMIN_WIDE))
async def test_an_admin_wide_change_locks_and_updates_its_users_by_primary_key_only(recorder_db, user_targets, action):
    before = UserStatus.disabled if action == "activate_all" else UserStatus.active
    admin_id, user_ids = await _seed(recorder_db, before)
    await seed_users_and_nodes(recorder_db, user_count=2, node_count=0)
    user_targets.clear()

    await _admin_wide(recorder_db, admin_id, action)

    locks = [columns for kind, columns in user_targets if kind == "lock"]
    writes = [columns for kind, columns in user_targets if kind == "write"]
    assert locks and writes, user_targets
    assert all(columns == {"id"} for columns in locks), user_targets
    assert all("id" in columns and "admin_id" not in columns for columns in writes), user_targets
    after = UserStatus.active if action == "activate_all" else UserStatus.disabled
    assert await _final(recorder_db, user_ids) == [(after, 0)] * USERS


def _hold_once(monkeypatch, module, name: str, started: list[asyncio.Task], start) -> None:
    real = getattr(module, name)

    async def hold(*args, **kwargs):
        result = await real(*args, **kwargs)
        if not started:
            started.append(asyncio.create_task(start()))
            await asyncio.sleep(HOLD_SECONDS)
        return result

    monkeypatch.setattr(module, name, hold)


async def _outcome(step) -> str:
    try:
        await step()
        return "committed"
    except HTTPException as exc:
        return f"answered {exc.status_code}"
    except Exception as exc:
        return f"aborted: {type(exc).__name__}: {getattr(exc, 'orig', exc)!r}"[:300]


@server_database_only
@pytest.mark.asyncio
@pytest.mark.parametrize("held", [2, 4], ids=["early-user", "late-user"])
async def test_an_admin_wide_activate_arriving_while_a_toggle_holds_one_of_its_users_commits_both_without_a_deadlock(
    monkeypatch: pytest.MonkeyPatch, recorder_db, held
):
    admin_id, user_ids = await _seed(recorder_db, UserStatus.disabled)
    started: list[asyncio.Task] = []
    _hold_once(
        monkeypatch,
        user_operation,
        "lock_user_rows",
        started,
        lambda: _outcome(lambda: _admin_wide(recorder_db, admin_id, "activate_all")),
    )

    async def toggle():
        async with recorder_db() as db:
            db_user = await get_user_by_id(db, user_ids[held], load_admin_role=True)
            await UserOperation(OperatorType.API)._set_user_disabled(
                db, db_user, UserStatusToggle(disabled=False), OWNER
            )

    deadlocks_before = await _deadlocks()
    toggled = await _outcome(toggle)
    admin_wide = await asyncio.wait_for(started[0], timeout=60)
    deadlocks_after = await _deadlocks()

    assert (toggled, admin_wide) == ("committed", "committed")
    if deadlocks_before is not None:
        assert deadlocks_after - deadlocks_before == 0
    assert await _final(recorder_db, user_ids) == [(UserStatus.active, 0)] * USERS


@server_database_only
@pytest.mark.asyncio
@pytest.mark.parametrize("held", [2, 4], ids=["early-user", "late-user"])
async def test_an_admin_wide_disable_arriving_while_the_enforcer_holds_one_of_its_users_commits_both_without_a_deadlock(
    monkeypatch: pytest.MonkeyPatch, recorder_db, held
):
    admin_id, user_ids = await _seed(recorder_db, UserStatus.active)
    started: list[asyncio.Task] = []
    _hold_once(
        monkeypatch,
        connection_limiter,
        "restrict",
        started,
        lambda: _outcome(lambda: _admin_wide(recorder_db, admin_id, "disable_all")),
    )
    observation = SimpleNamespace(
        user_id=user_ids[held], verdict="over_limit", streak=5, devices=4, limit_applied=2, details={}, reasons=["x"]
    )

    async def enforce():
        async with recorder_db() as db:
            await connection_limiter._enforce(db, [observation], SETTINGS)

    deadlocks_before = await _deadlocks()
    enforced = await _outcome(enforce)
    admin_wide = await asyncio.wait_for(started[0], timeout=60)
    deadlocks_after = await _deadlocks()

    assert (enforced, admin_wide) == ("committed", "committed")
    if deadlocks_before is not None:
        assert deadlocks_after - deadlocks_before == 0
    final = await _final(recorder_db, user_ids)
    assert [status for status, _active in final] == [UserStatus.disabled] * USERS
    assert sum(active for _status, active in final) == 0
