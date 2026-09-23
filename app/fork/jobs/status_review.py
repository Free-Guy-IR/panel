from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import GetDB
from app.db.crud.user import _review_user_select_stmt, start_users_expire
from app.db.models import User, UserStatus

USER_TRANSITION_BATCH_SIZE = 500
EXPIRABLE = (User.status == UserStatus.active, User.is_expired)
STARTABLE = (User.status == UserStatus.on_hold, User.become_online)


async def lock_users_matching(db: AsyncSession, user_ids: list[int], predicates: tuple) -> list[User]:
    locked_ids = list(
        (
            await db.execute(
                select(User.id).where(User.id.in_(user_ids), *predicates).order_by(User.id).with_for_update()
            )
        )
        .scalars()
        .all()
    )
    if not locked_ids:
        return []
    stmt = _review_user_select_stmt().where(User.id.in_(locked_ids)).execution_options(populate_existing=True)
    return list((await db.execute(stmt)).unique().scalars().all())


def _batches(user_ids: Iterable[int]) -> list[list[int]]:
    ordered = sorted(set(user_ids))
    return [ordered[i : i + USER_TRANSITION_BATCH_SIZE] for i in range(0, len(ordered), USER_TRANSITION_BATCH_SIZE)]


async def expire_users_under_lock(user_ids: Iterable[int]) -> None:
    from app.jobs.review_users import apply_status_changes

    batches = _batches(user_ids)
    if not batches:
        return

    async with GetDB() as db:
        for batch in batches:
            users = await lock_users_matching(db, batch, EXPIRABLE)
            await apply_status_changes(db, [user for user in users if user.next_plan is None], UserStatus.expired)
            await db.commit()
            for user_id in [user.id for user in users if user.next_plan is not None]:
                await apply_status_changes(db, await lock_users_matching(db, [user_id], EXPIRABLE), UserStatus.expired)
                await db.commit()


async def start_on_hold_users_under_lock(user_ids: Iterable[int]) -> None:
    from app.jobs.review_users import apply_status_changes

    batches = _batches(user_ids)
    if not batches:
        return

    async with GetDB() as db:
        for batch in batches:
            users = await lock_users_matching(db, batch, STARTABLE)
            if not users:
                await db.commit()
                continue
            await apply_status_changes(db, await start_users_expire(db, users), UserStatus.active)
