from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import GetDB
from app.db.crud.user import _review_user_select_stmt
from app.db.models import User, UserStatus

USER_LIMIT_BATCH_SIZE = 500
LIMITABLE_STATUSES = (UserStatus.active, UserStatus.on_hold)


async def _lock_limitable_users(db: AsyncSession, user_ids: list[int], statuses: tuple[UserStatus, ...]) -> list[User]:
    locked_ids = list(
        (
            await db.execute(
                select(User.id)
                .where(User.id.in_(user_ids), User.status.in_(statuses), User.is_limited)
                .order_by(User.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    if not locked_ids:
        return []
    stmt = _review_user_select_stmt().where(User.id.in_(locked_ids)).execution_options(populate_existing=True)
    return list((await db.execute(stmt)).unique().scalars().all())


async def limit_users_under_lock(user_ids: Iterable[int], statuses: Iterable[UserStatus]) -> None:
    from app.fork.jobs.status_review import renew_next_plan_users
    from app.jobs.review_users import apply_status_changes

    ordered_ids = sorted(set(user_ids))
    allowed = tuple(statuses)
    if not ordered_ids:
        return

    async def lock_limitable(db: AsyncSession, ids: list[int]) -> list[User]:
        return await _lock_limitable_users(db, ids, allowed)

    async with GetDB() as db:
        for start in range(0, len(ordered_ids), USER_LIMIT_BATCH_SIZE):
            users = await _lock_limitable_users(db, ordered_ids[start : start + USER_LIMIT_BATCH_SIZE], allowed)
            await apply_status_changes(db, [user for user in users if user.next_plan is None], UserStatus.limited)
            await db.commit()
            await renew_next_plan_users(
                db, [user.id for user in users if user.next_plan is not None], lock_limitable, UserStatus.limited
            )


async def enforce_user_limits_now(user_ids: Iterable[int]) -> None:
    await limit_users_under_lock(user_ids, LIMITABLE_STATUSES)
