from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import Boolean, DateTime, Integer, cast, false, func, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User
from app.fork.crud.user_locks import on_primary_key

STORABLE_EXPIRE_MIN = datetime(1000, 1, 1, tzinfo=UTC)
STORABLE_EXPIRE_MAX = datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)
STORABLE_DATA_LIMIT_MAX = 2**63 - 1
STORABLE_EXPIRE_MIN_EPOCH = int(STORABLE_EXPIRE_MIN.timestamp())
STORABLE_EXPIRE_MAX_EPOCH = int(STORABLE_EXPIRE_MAX.replace(microsecond=0).timestamp())


class BulkUserResultOutOfRange(ValueError):
    pass


def out_of_range_detail(field: Literal["expire", "data_limit"]) -> str:
    if field == "expire":
        return (
            f"Bulk expire would move a selected user's expire outside "
            f"{STORABLE_EXPIRE_MIN.date()}..{STORABLE_EXPIRE_MAX.date()}; nothing was changed"
        )
    return (
        f"Bulk data limit would move a selected user's data limit above {STORABLE_DATA_LIMIT_MAX}; nothing was changed"
    )


def sqlite_expire_seconds(column):
    return cast(func.strftime("%s", column), Integer)


def expire_as_bulk_reads_it(db: AsyncSession):
    if _dialect(db) == "sqlite":
        return func.datetime(sqlite_expire_seconds(User.expire), "unixepoch", type_=DateTime)
    return User.expire


def bulk_result_out_of_range(field: Literal["expire", "data_limit"], amount: int, dialect: str | None = None):
    if field == "expire":
        if amount == 0:
            return false()
        if dialect == "sqlite":
            moved = sqlite_expire_seconds(User.expire) + amount
            beyond = moved < STORABLE_EXPIRE_MIN_EPOCH if amount < 0 else moved > STORABLE_EXPIRE_MAX_EPOCH
            return func.coalesce(beyond, true(), type_=Boolean)
        if amount > 0:
            return User.expire > STORABLE_EXPIRE_MAX - timedelta(seconds=amount)
        return User.expire < STORABLE_EXPIRE_MIN - timedelta(seconds=amount)
    if amount > 0:
        return User.data_limit > STORABLE_DATA_LIMIT_MAX - amount
    return false()


def _dialect(db: AsyncSession) -> str:
    return db.get_bind().dialect.name


def bulk_result_guard(db: AsyncSession, field: Literal["expire", "data_limit"], amount: int):
    return bulk_result_out_of_range(field, amount, _dialect(db))


async def out_of_range_result_exists(
    db: AsyncSession, field: Literal["expire", "data_limit"], amount: int, *conditions, lock: bool
) -> bool:
    stmt = select(User.id).where(*conditions, bulk_result_guard(db, field, amount)).limit(1)
    if lock:
        stmt = on_primary_key(stmt).with_for_update()
    return (await db.execute(stmt)).first() is not None


def bulk_final_value(field: Literal["expire", "data_limit"], value, amount: int):
    try:
        final = value + timedelta(seconds=amount) if field == "expire" else value + amount
    except OverflowError:
        return None
    if field == "expire":
        return final if STORABLE_EXPIRE_MIN <= final <= STORABLE_EXPIRE_MAX else None
    return final if final <= STORABLE_DATA_LIMIT_MAX else None
