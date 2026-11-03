from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import false, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User

STORABLE_EXPIRE_MIN = datetime(1000, 1, 1, tzinfo=UTC)
STORABLE_EXPIRE_MAX = datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)
STORABLE_DATA_LIMIT_MAX = 2**63 - 1


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


def bulk_result_out_of_range(field: Literal["expire", "data_limit"], amount: int):
    if field == "expire":
        if amount > 0:
            return User.expire > STORABLE_EXPIRE_MAX - timedelta(seconds=amount)
        if amount < 0:
            return User.expire < STORABLE_EXPIRE_MIN - timedelta(seconds=amount)
        return false()
    if amount > 0:
        return User.data_limit > STORABLE_DATA_LIMIT_MAX - amount
    return false()


async def count_out_of_range_results(
    db: AsyncSession, field: Literal["expire", "data_limit"], amount: int, *conditions
) -> int:
    stmt = select(func.count(User.id)).where(*conditions, bulk_result_out_of_range(field, amount))
    return (await db.execute(stmt)).scalar_one_or_none() or 0


def bulk_final_value(field: Literal["expire", "data_limit"], value, amount: int):
    try:
        final = value + timedelta(seconds=amount) if field == "expire" else value + amount
    except OverflowError:
        return None
    if field == "expire":
        return final if STORABLE_EXPIRE_MIN <= final <= STORABLE_EXPIRE_MAX else None
    return final if final <= STORABLE_DATA_LIMIT_MAX else None
