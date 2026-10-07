from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import Column, event, select, update
from sqlalchemy.orm import Session
from sqlalchemy.sql import visitors
from sqlalchemy.sql.util import find_tables

from app.db.models import User, UserStatus
from app.fork.crud.user_locks import lock_user_rows
from app.fork.jobs import connection_limiter
from app.fork.models.connection import ConnectionRestriction
from app.models.settings import ConnectionLimit
from app.utils import connection_enforcement
from app.utils.connection_enforcement import close_active_restrictions, due_to_restore
from tests.test_recorder_harness import _database_url, recorder_db_fixture, seed_users_and_nodes  # noqa: F401
from tests.test_status_lock_order_interleavings import _deadlocks

SETTINGS = ConnectionLimit(
    enabled=True, monitor_only=False, enforcement_enabled=True, persistence_cycles=1, punishment_steps=[30]
)
HELD_SECONDS = 3
server_database_only = pytest.mark.skipif(
    _database_url().startswith("sqlite"), reason="needs a server database with row locks"
)


@pytest.fixture(name="restriction_targets")
def restriction_targets_fixture():
    seen: list[tuple[str, set[str]]] = []

    def on_orm(state):
        statement = state.statement
        if state.is_select and statement._for_update_arg is not None:
            kind = "lock"
            tables = {table.name for from_ in statement.get_final_froms() for table in find_tables(from_)}
        elif state.is_update or state.is_delete:
            kind = "write"
            tables = {statement.table.name}
        else:
            return
        if "connection_restrictions" not in tables:
            return
        where = statement.whereclause
        columns = (
            set()
            if where is None
            else {
                element.key
                for element in visitors.iterate(where)
                if isinstance(element, Column) and element.table.name == "connection_restrictions"
            }
        )
        seen.append((kind, columns))

    event.listen(Session, "do_orm_execute", on_orm)
    yield seen
    event.remove(Session, "do_orm_execute", on_orm)


async def _restricted(recorder_db, count: int, *, due: bool = True) -> tuple[int, list[int]]:
    admin_id, user_ids, _ = await seed_users_and_nodes(recorder_db, user_count=count, node_count=0)
    restore_at = datetime.now(UTC) + (timedelta(seconds=-1) if due else timedelta(minutes=30))
    async with recorder_db() as session:
        await session.execute(update(User).where(User.id.in_(user_ids)).values(status=UserStatus.disabled))
        session.add_all(
            ConnectionRestriction(
                user_id=user_id,
                previous_status=UserStatus.active.value,
                disable_minutes=30,
                restore_at=restore_at,
                active=True,
            )
            for user_id in user_ids
        )
        await session.commit()
    return admin_id, user_ids


async def _active_restrictions(recorder_db, user_ids) -> dict[int, int]:
    async with recorder_db() as session:
        rows = await session.execute(
            select(ConnectionRestriction.user_id).where(
                ConnectionRestriction.user_id.in_(list(user_ids)), ConnectionRestriction.active.is_(True)
            )
        )
        found = [user_id for (user_id,) in rows.all()]
    return {user_id: found.count(user_id) for user_id in user_ids}


@pytest.mark.asyncio
async def test_closing_restrictions_targets_their_rows_by_primary_key_only(recorder_db, restriction_targets):
    _, user_ids = await _restricted(recorder_db, 3)
    restriction_targets.clear()

    async with recorder_db() as db:
        await lock_user_rows(db, user_ids)
        closed = await close_active_restrictions(db, user_ids, "test")
        await db.commit()

    assert closed == 3
    assert restriction_targets, "closing took no lock and wrote nothing"
    assert all("user_id" not in columns and "id" in columns for _kind, columns in restriction_targets), (
        restriction_targets
    )
    assert await _active_restrictions(recorder_db, user_ids) == dict.fromkeys(user_ids, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("everyone", [False, True], ids=["due", "enforcement-off"])
async def test_the_release_locks_restriction_rows_by_primary_key_only(recorder_db, restriction_targets, everyone):
    _, due_ids = await _restricted(recorder_db, 2)
    _, running_ids = await _restricted(recorder_db, 1, due=False)
    restriction_targets.clear()

    async with recorder_db() as db:
        pairs = await due_to_restore(db, everyone=everyone)
        released = sorted(user.id for _restriction, user in pairs)
        await db.rollback()

    assert released == sorted(due_ids + (running_ids if everyone else []))
    locks = [columns for kind, columns in restriction_targets if kind == "lock"]
    assert locks, restriction_targets
    assert all("user_id" not in columns and "id" in columns for columns in locks), restriction_targets


@pytest.mark.asyncio
async def test_forgetting_old_violations_deletes_their_rows_by_primary_key_only(recorder_db, restriction_targets):
    _, user_ids = await _restricted(recorder_db, 3)
    async with recorder_db() as session:
        await session.execute(
            update(ConnectionRestriction)
            .where(ConnectionRestriction.user_id.in_(user_ids[:2]))
            .values(active=False, created_at=datetime.now(UTC) - timedelta(days=40))
        )
        await session.commit()
    restriction_targets.clear()

    async with recorder_db() as db:
        forgotten = await connection_enforcement.forget_old(db, 30)
        await db.commit()

    assert forgotten == 2
    deletes = [columns for kind, columns in restriction_targets if kind == "write"]
    assert deletes, restriction_targets
    assert all(columns == {"id"} for columns in deletes), restriction_targets
    async with recorder_db() as session:
        left = (await session.execute(select(ConnectionRestriction.user_id))).scalars().all()
    assert sorted(left) == user_ids[2:]


async def _close_and_hold(recorder_db, user_id: int, holding: asyncio.Event, proceed: asyncio.Event) -> None:
    async with recorder_db() as db:
        await lock_user_rows(db, [user_id])
        await close_active_restrictions(db, [user_id], "an admin set the user's status")
        holding.set()
        await proceed.wait()
        await db.commit()


async def _release_and_hold(recorder_db, _user_id: int, holding: asyncio.Event, proceed: asyncio.Event) -> None:
    async with recorder_db() as db:
        pairs = await due_to_restore(db)
        for restriction, user in pairs:
            connection_enforcement.release(restriction, user)
        holding.set()
        await proceed.wait()
        await db.commit()


async def _enforce(recorder_db, user_id: int) -> None:
    observation = SimpleNamespace(
        user_id=user_id, verdict="over_limit", streak=5, devices=4, limit_applied=2, details={}, reasons=["held"]
    )
    async with recorder_db() as db:
        await connection_limiter._enforce(db, [observation], SETTINGS)


@server_database_only
@pytest.mark.asyncio
@pytest.mark.parametrize("holder", [_close_and_hold, _release_and_hold], ids=["closing", "releasing"])
async def test_restricting_a_user_never_waits_for_another_users_restriction_being_closed_or_released(
    monkeypatch: pytest.MonkeyPatch, recorder_db, holder
):
    _, (held_id,) = await _restricted(recorder_db, 1)
    _, (enforced_id,), _ = await seed_users_and_nodes(recorder_db, user_count=1, node_count=0)
    pushed: list[int] = []

    async def record_push(users):
        pushed.extend(user.id for user in users)

    monkeypatch.setattr(connection_limiter, "_push_to_nodes", record_push)
    deadlocks_before = await _deadlocks()
    holding, proceed = asyncio.Event(), asyncio.Event()
    holder_task = asyncio.create_task(holder(recorder_db, held_id, holding, proceed))
    await asyncio.wait_for(holding.wait(), timeout=30)

    enforcer = asyncio.create_task(_enforce(recorder_db, enforced_id))
    done, _ = await asyncio.wait({enforcer}, timeout=HELD_SECONDS)
    finished_while_held = enforcer in done
    proceed.set()
    await asyncio.wait_for(asyncio.gather(holder_task, enforcer), timeout=90)
    deadlocks_after = await _deadlocks()

    assert finished_while_held, "the enforcer's insert waited for a restriction lock taken for a different user"
    if deadlocks_before is not None:
        assert deadlocks_after - deadlocks_before == 0
    assert pushed == [enforced_id]
    assert await _active_restrictions(recorder_db, [held_id, enforced_id]) == {held_id: 0, enforced_id: 1}
