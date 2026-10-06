from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, update
from sqlalchemy.orm import Session
from sqlalchemy.sql.util import find_tables

from app.db.models import User, UserStatus
from app.fork.models.connection import ConnectionRestriction
from app.utils.connection_enforcement import due_to_restore
from tests.test_recorder_harness import recorder_db_fixture, seed_users_and_nodes  # noqa: F401


@pytest.fixture(name="row_locks")
def row_locks_fixture():
    locks: list[list[str]] = []

    def on_orm(state):
        statement = state.statement
        if state.is_select and statement._for_update_arg is not None:
            locks.append(sorted({table.name for from_ in statement.get_final_froms() for table in find_tables(from_)}))

    event.listen(Session, "do_orm_execute", on_orm)
    yield locks
    event.remove(Session, "do_orm_execute", on_orm)


async def _restricted(recorder_db, count: int, *, due: bool) -> list[int]:
    _, user_ids, _ = await seed_users_and_nodes(recorder_db, user_count=count, node_count=0)
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
            for user_id in reversed(user_ids)
        )
        await session.commit()
    return user_ids


@pytest.mark.asyncio
@pytest.mark.parametrize("everyone", [False, True], ids=["due", "enforcement-off"])
async def test_the_release_locks_user_rows_before_their_restrictions(recorder_db, row_locks, everyone):
    due_ids = await _restricted(recorder_db, 3, due=True)
    running_ids = await _restricted(recorder_db, 1, due=False)
    row_locks.clear()

    async with recorder_db() as session:
        pairs = await due_to_restore(session, everyone=everyone)
        released = sorted(user.id for _restriction, user in pairs)
        await session.rollback()

    assert released == sorted(due_ids + (running_ids if everyone else []))
    assert row_locks, "the release took no row locks"
    assert all(len(tables) == 1 for tables in row_locks), (
        f"a single locking read took restriction and user rows together, in the join's order: {row_locks}"
    )
    order = [tables[0] for tables in row_locks]
    assert "users" in order and "connection_restrictions" in order, order
    assert order.index("users") < order.index("connection_restrictions"), (
        f"the release locked restrictions before users, the reverse of enforcement and the admin toggle: {order}"
    )
    assert order[order.index("connection_restrictions") :].count("users") == 0, order


@pytest.mark.asyncio
async def test_the_release_skips_a_restriction_closed_after_its_candidates_were_read(recorder_db, monkeypatch):
    from app.utils import connection_enforcement

    (closed_id, kept_id) = await _restricted(recorder_db, 2, due=True)
    real_lock = connection_enforcement._lock_user_rows

    async def close_one_first(db, user_ids):
        async with recorder_db() as admin:
            await admin.execute(
                update(ConnectionRestriction)
                .where(ConnectionRestriction.user_id == closed_id)
                .values(active=False, restored_at=datetime.now(UTC))
            )
            await admin.commit()
        return await real_lock(db, user_ids)

    monkeypatch.setattr(connection_enforcement, "_lock_user_rows", close_one_first)

    async with recorder_db() as session:
        pairs = await due_to_restore(session)
        released = [user.id for _restriction, user in pairs]
        await session.rollback()

    assert released == [kept_id]
