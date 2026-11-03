from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select, update
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


@pytest.mark.asyncio
@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("field,amount,seed,moved", CASES)
async def test_a_separate_connection_writer_after_the_locked_check_never_lands_an_out_of_range_result(
    wal_sqlite, monkeypatch, strict, field, amount, seed, moved
):
    _, user_ids, _ = await seed_users_and_nodes(wal_sqlite, user_count=2, node_count=0)
    async with wal_sqlite() as session:
        await session.execute(
            update(User).where(User.id.in_(user_ids)).values(status=UserStatus.active, used_traffic=MB, **seed)
        )
        await session.commit()
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

    assert len(writer) == 1, writer
    if field == "expire":
        assert all(value is not None for value in rows.values()), (writer, outcome, rows)
    else:
        assert all(isinstance(value, int) and 0 <= value <= BIGINT_MAX for value in rows.values()), (
            writer,
            outcome,
            rows,
        )
    if writer == ["landed"]:
        assert outcome in ("rejected", "conflict"), (outcome, rows)
    else:
        assert outcome == "applied", (outcome, rows)
