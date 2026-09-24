from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import GetDB
from app.db.crud.user import DELETE_CHUNK_SIZE, _review_user_select_stmt, start_users_expire, update_users_status
from app.db.models import User, UserStatus

USER_TRANSITION_BATCH_SIZE = 500
EXPIRABLE = (User.status == UserStatus.active, User.is_expired)
STARTABLE = (User.status == UserStatus.on_hold, User.become_online)
CAME_ONLINE = (*STARTABLE, User.online_at.isnot(None))
TIMED_OUT = (*STARTABLE, User.online_at.is_(None))


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


def _batches(user_ids: Iterable[int], size: int = USER_TRANSITION_BATCH_SIZE) -> list[list[int]]:
    ordered = sorted(set(user_ids))
    return [ordered[i : i + size] for i in range(0, len(ordered), size)]


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


async def start_on_hold_users_under_lock(user_ids: Iterable[int], predicates: tuple = STARTABLE) -> None:
    from app.jobs.review_users import apply_status_changes

    batches = _batches(user_ids)
    if not batches:
        return

    async with GetDB() as db:
        for batch in batches:
            users = await lock_users_matching(db, batch, predicates)
            if not users:
                await db.commit()
                continue
            await apply_status_changes(db, await start_users_expire(db, users), UserStatus.active)


async def disable_on_hold_users_under_lock(user_ids: Iterable[int]) -> None:
    from app.jobs.review_users import apply_status_changes

    batches = _batches(user_ids)
    if not batches:
        return

    await start_on_hold_users_under_lock([user_id for batch in batches for user_id in batch], CAME_ONLINE)
    async with GetDB() as db:
        for batch in batches:
            users = await lock_users_matching(db, batch, TIMED_OUT)
            if not users:
                await db.commit()
                continue
            disabled = await update_users_status(db, users, UserStatus.disabled)
            await apply_status_changes(db, disabled, UserStatus.disabled)


async def remove_on_hold_users_under_lock(user_ids: Iterable[int]) -> None:
    from app.jobs.review_users import remove_on_hold_users

    batches = _batches(user_ids, DELETE_CHUNK_SIZE)
    if not batches:
        return

    await start_on_hold_users_under_lock([user_id for batch in batches for user_id in batch], CAME_ONLINE)
    async with GetDB() as db:
        for batch in batches:
            users = await lock_users_matching(db, batch, TIMED_OUT)
            if not users:
                await db.commit()
                continue
            await remove_on_hold_users(db, users)
