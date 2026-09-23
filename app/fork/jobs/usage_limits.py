from collections.abc import Iterable

from app.db import GetDB
from app.db.crud.user import _review_user_select_stmt
from app.db.models import User, UserStatus

USER_LIMIT_BATCH_SIZE = 500
LIMITABLE_STATUSES = (UserStatus.active, UserStatus.on_hold)


async def enforce_user_limits_now(user_ids: Iterable[int]) -> None:
    from app.jobs.review_users import apply_status_changes

    ordered_ids = sorted(set(user_ids))
    if not ordered_ids:
        return

    async with GetDB() as db:
        for start in range(0, len(ordered_ids), USER_LIMIT_BATCH_SIZE):
            batch = ordered_ids[start : start + USER_LIMIT_BATCH_SIZE]
            stmt = _review_user_select_stmt().where(
                User.id.in_(batch),
                User.status.in_(LIMITABLE_STATUSES),
                User.is_limited,
            )
            users = list((await db.execute(stmt)).unique().scalars().all())
            await apply_status_changes(db, users, UserStatus.limited)
