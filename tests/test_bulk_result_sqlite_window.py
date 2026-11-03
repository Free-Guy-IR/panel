from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.db.crud.bulk as bulk_crud
from app.db import base
from app.db.models import User, UserStatus
from app.fork.crud.bulk_result_bounds import BulkUserResultOutOfRange
from app.models.user import BulkUser
from tests.test_recorder_harness import seed_users_and_nodes

MB = 1024 * 1024
BIGINT_MAX = 2**63 - 1
DATA_STEP = 2**62
EXPIRE_STEP = 10**10
FAR_FUTURE = datetime(9800, 1, 1, tzinfo=UTC)
EDGE = datetime(9999, 12, 31, 23, 59, 57, tzinfo=UTC)


@pytest.fixture
async def wal_sqlite(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/bulk_window.db", poolclass=NullPool, connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _pragmas(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=300")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(base.Base.metadata.create_all)
    yield async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    await engine.dispose()


CASES = [
    (
        "expire",
        EXPIRE_STEP,
        {"expire": datetime.now(UTC).replace(microsecond=0) + timedelta(hours=2)},
        {"expire": FAR_FUTURE},
    ),
    ("expire", 2, {"expire": EDGE}, {"expire": EDGE + timedelta(seconds=1)}),
    ("data_limit", DATA_STEP, {"data_limit": 5 * MB}, {"data_limit": DATA_STEP}),
]


async def _seed(wal_sqlite, **values) -> list[int]:
    _, user_ids, _ = await seed_users_and_nodes(wal_sqlite, user_count=2, node_count=0)
    async with wal_sqlite() as session:
        await session.execute(
            update(User).where(User.id.in_(user_ids)).values(status=UserStatus.active, used_traffic=MB, **values)
        )
        await session.commit()
    return user_ids


async def _no_write_lock(db) -> None:
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize("write_lock", [True, False])
@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("field,amount,seed,moved", CASES)
async def test_a_separate_connection_writer_after_the_locked_check_never_lands_an_out_of_range_result(
    wal_sqlite, monkeypatch, write_lock, strict, field, amount, seed, moved
):
    user_ids = await _seed(wal_sqlite, **seed)
    if not write_lock:
        monkeypatch.setattr(bulk_crud, "_hold_write_lock_without_row_locks", _no_write_lock)
    original = bulk_crud._reject_out_of_range_locked_targets
    writer: list[str] = []

    async def check_then_race(*args, **kwargs):
        await original(*args, **kwargs)
        try:
            async with wal_sqlite() as other:
                await other.execute(update(User).where(User.id == user_ids[1]).values(**moved))
                await other.commit()
            writer.append("landed")
        except OperationalError as exc:
            assert "locked" in str(exc), exc
            writer.append("blocked")

    monkeypatch.setattr(bulk_crud, "_reject_out_of_range_locked_targets", check_then_race)
    bulk = BulkUser(amount=amount, users=user_ids)
    run = bulk_crud.update_users_expire if field == "expire" else bulk_crud.update_users_datalimit
    outcome = "applied"
    async with wal_sqlite() as session:
        try:
            await run(session, bulk, target_ids=user_ids if strict else None)
        except BulkUserResultOutOfRange:
            outcome = "rejected"
        except bulk_crud.BulkUserUpdateConflict:
            outcome = "conflict"
    async with wal_sqlite() as session:
        rows = dict((await session.execute(select(User.id, getattr(User, field)).where(User.id.in_(user_ids)))).all())

    assert writer == (["blocked"] if write_lock else ["landed"]), (writer, outcome, rows)
    if field == "expire":
        assert all(value is not None for value in rows.values()), (writer, outcome, rows)
    else:
        assert all(isinstance(value, int) and 0 <= value <= BIGINT_MAX for value in rows.values()), (
            writer,
            outcome,
            rows,
        )
    if write_lock:
        assert outcome == "applied", (outcome, rows)
    else:
        assert outcome in ("rejected", "conflict"), (outcome, rows)
        kept = {user_ids[0]: seed[field], user_ids[1]: moved[field]}
        assert rows == {
            user_id: value.replace(tzinfo=None) if isinstance(value, datetime) else value
            for user_id, value in kept.items()
        }, (outcome, rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", [60, -60])
async def test_a_user_already_loaded_in_the_session_does_not_break_the_bulk_expire(wal_sqlite, amount):
    start = datetime(2030, 1, 1, tzinfo=UTC)
    user_ids = await _seed(wal_sqlite, expire=start)
    async with wal_sqlite() as session:
        loaded = (await session.execute(select(User).where(User.id == user_ids[0]))).scalar_one()
        assert loaded.id == user_ids[0]
        _, count = await bulk_crud.update_users_expire(session, BulkUser(amount=amount, users=user_ids))
    assert count == 2
    async with wal_sqlite() as session:
        stored = (await session.execute(select(User.expire).where(User.id.in_(user_ids)))).scalars().all()
    assert stored == [(start + timedelta(seconds=amount)).replace(tzinfo=None)] * 2, stored


@pytest.mark.asyncio
async def test_an_unreadable_sqlite_expire_rejects_a_negative_bulk_step_instead_of_skipping_it(wal_sqlite):
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=2)
    user_ids = await _seed(wal_sqlite, expire=start)
    async with wal_sqlite() as session:
        await session.execute(text("UPDATE users SET expire = 'not a date' WHERE id = :id"), {"id": user_ids[1]})
        await session.commit()
    async with wal_sqlite() as session:
        with pytest.raises(BulkUserResultOutOfRange):
            await bulk_crud.update_users_expire(session, BulkUser(amount=-60, users=user_ids))
    async with wal_sqlite() as session:
        stored = (
            await session.execute(
                text("SELECT id, expire FROM users WHERE id IN (:a, :b)"), {"a": user_ids[0], "b": user_ids[1]}
            )
        ).all()
    assert dict(stored)[user_ids[1]] == "not a date", stored
