import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal

import pytest
from sqlalchemy import event, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.db.crud.bulk as bulk_crud
from app.db import base
from app.db.models import User, UserStatus
from app.fork.crud.bulk_result_bounds import (
    STORABLE_EXPIRE_MAX,
    STORABLE_EXPIRE_MIN,
    BulkUserResultOutOfRange,
    bulk_final_value,
)
from app.models.user import BulkUser
from app.utils.helpers import fix_datetime_timezone
from tests.test_recorder_harness import seed_users_and_nodes

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _epoch_seconds(value: str, rounding) -> str | None:
    try:
        parsed = datetime.fromisoformat(value)
    except TypeError, ValueError:
        return None
    delta = parsed.replace(tzinfo=UTC) - EPOCH
    seconds = Decimal(delta.days * 86400 + delta.seconds) + Decimal(delta.microseconds) / Decimal(10**6)
    if rounding == "round_ms":
        seconds = seconds.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    return str(int(seconds.to_integral_value(rounding=ROUND_FLOOR)))


def _strftime_reader(rounding):
    native = sqlite3.connect(":memory:", check_same_thread=False)

    def strftime(fmt, value, *modifiers):
        if fmt == "%s" and not modifiers and isinstance(value, str):
            return _epoch_seconds(value, rounding)
        return native.execute(
            f"SELECT strftime({', '.join('?' * (2 + len(modifiers)))})", (fmt, value, *modifiers)
        ).fetchone()[0]

    return strftime


@pytest.fixture(params=["native", "round_ms", "floor"])
async def edge_sqlite(request, tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/edge.db", poolclass=NullPool, connect_args={"check_same_thread": False}
    )
    if request.param != "native":
        reader = _strftime_reader(request.param)

        @event.listens_for(engine.sync_engine, "connect")
        def _override_strftime(dbapi_conn, _record):
            for arity in (2, 3, 4):
                dbapi_conn.create_function("strftime", arity, reader, deterministic=True)

    async with engine.begin() as conn:
        await conn.run_sync(base.Base.metadata.create_all)
    yield async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    await engine.dispose()


EDGE_CASES = [
    (STORABLE_EXPIRE_MIN + timedelta(seconds=2) - timedelta(microseconds=1), -2),
    (STORABLE_EXPIRE_MIN + timedelta(seconds=2) - timedelta(microseconds=400), -2),
    (STORABLE_EXPIRE_MIN + timedelta(seconds=2) - timedelta(microseconds=600), -2),
    (STORABLE_EXPIRE_MIN + timedelta(seconds=1, microseconds=500000), -1),
    (STORABLE_EXPIRE_MIN + timedelta(seconds=2), -2),
    (STORABLE_EXPIRE_MIN + timedelta(seconds=2), -3),
    (STORABLE_EXPIRE_MAX - timedelta(seconds=1, microseconds=400), 1),
    (STORABLE_EXPIRE_MAX - timedelta(seconds=1, microseconds=600), 1),
    (STORABLE_EXPIRE_MAX - timedelta(seconds=1, microseconds=600000), 1),
    (STORABLE_EXPIRE_MAX.replace(microsecond=0), 1),
]


async def _seed_expire(factory, expire: datetime) -> int:
    _, user_ids, _ = await seed_users_and_nodes(factory, user_count=1, node_count=0)
    async with factory() as session:
        await session.execute(update(User).where(User.id == user_ids[0]).values(status=UserStatus.active))
        await session.execute(
            text("UPDATE users SET expire = :expire WHERE id = :id"),
            {"expire": expire.replace(tzinfo=None).isoformat(sep=" "), "id": user_ids[0]},
        )
        await session.commit()
    return user_ids[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("seed,amount", EDGE_CASES)
async def test_the_owner_guard_and_the_limited_admin_check_agree_and_match_what_is_stored(edge_sqlite, seed, amount):
    user_id = await _seed_expire(edge_sqlite, seed)
    bulk = BulkUser(amount=amount, users=[user_id])
    async with edge_sqlite() as session:
        owner_rejects = await bulk_crud.bulk_out_of_range_target_exists(session, bulk, "expire")
        targets = await bulk_crud.get_bulk_user_limit_targets(session, bulk, "expire", lock=False)
    limited_rejects = any(
        bulk_final_value("expire", fix_datetime_timezone(value), amount) is None for _, value in targets
    )
    assert [user for user, _ in targets] == [user_id]
    assert owner_rejects == limited_rejects, (seed, amount, owner_rejects, targets)

    async with edge_sqlite() as session:
        if owner_rejects:
            with pytest.raises(BulkUserResultOutOfRange):
                await bulk_crud.update_users_expire(session, bulk)
        else:
            _, count = await bulk_crud.update_users_expire(session, bulk)
            assert count == 1
    async with edge_sqlite() as session:
        stored = (await session.execute(text("SELECT expire FROM users WHERE id = :id"), {"id": user_id})).scalar_one()
        moved = (await session.execute(select(User.expire).where(User.id == user_id))).scalar_one()
    assert stored is not None, (seed, amount)
    assert STORABLE_EXPIRE_MIN.replace(tzinfo=None) <= moved <= STORABLE_EXPIRE_MAX.replace(tzinfo=None), stored
    if not owner_rejects:
        assert moved == targets[0][1] + timedelta(seconds=amount), (stored, targets)
