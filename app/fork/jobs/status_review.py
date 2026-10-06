import asyncio
from collections.abc import Awaitable, Callable, Iterable

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import GetDB
from app.db.crud.user import (
    DELETE_CHUNK_SIZE,
    _delete_locked_users,
    _review_user_select_stmt,
    start_users_expire,
    update_users_status,
)
from app.db.models import User, UserStatus
from app.fork.proxy_secrets.errors import describe_failure
from app.fork.proxy_secrets.locks import (
    PROXY_SECRETS_LOCK,
    NamedLockMissing,
    acquire_proxy_secrets_lock,
    is_deadlock,
    is_lock_unavailable,
)
from app.node.sync import remove_users as sync_remove_users

USER_TRANSITION_BATCH_SIZE = 500
NEXT_PLAN_ATTEMPTS = 3
NEXT_PLAN_POSTPONEMENTS_BEFORE_ERROR = 5
_postponed_renewals: dict[int, int] = {}
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


async def renew_next_plan_users(
    db: AsyncSession,
    user_ids: Iterable[int],
    lock_users: Callable[[AsyncSession, list[int]], Awaitable[list[User]]],
    status: UserStatus,
) -> None:
    from app.jobs.review_users import apply_status_changes, logger

    for user_id in user_ids:
        for attempt in range(1, NEXT_PLAN_ATTEMPTS + 1):
            try:
                await acquire_proxy_secrets_lock(db)
                await apply_status_changes(db, await lock_users(db, [user_id]), status)
                await db.commit()
                _postponed_renewals.pop(user_id, None)
                break
            except (DBAPIError, NamedLockMissing) as exc:
                await db.rollback()
                if not is_lock_unavailable(exc):
                    raise
                if is_deadlock(exc) and attempt < NEXT_PLAN_ATTEMPTS:
                    continue
                runs = _postponed_renewals[user_id] = _postponed_renewals.get(user_id, 0) + 1
                message = (
                    f"next plan of user {user_id} was postponed on {runs} consecutive run(s) because the "
                    f"{PROXY_SECRETS_LOCK!r} named lock or the user's row stayed unavailable after {attempt} "
                    f"attempt(s); the user stays eligible and the next run retries it ({describe_failure(exc)})"
                )
                if runs >= NEXT_PLAN_POSTPONEMENTS_BEFORE_ERROR:
                    logger.error(message)
                else:
                    logger.warning(message)
                break


async def expire_users_under_lock(user_ids: Iterable[int]) -> None:
    from app.jobs.review_users import apply_status_changes

    batches = _batches(user_ids)
    if not batches:
        return

    async def lock_expirable(db: AsyncSession, ids: list[int]) -> list[User]:
        return await lock_users_matching(db, ids, EXPIRABLE)

    async with GetDB() as db:
        for batch in batches:
            users = await lock_users_matching(db, batch, EXPIRABLE)
            await apply_status_changes(db, [user for user in users if user.next_plan is None], UserStatus.expired)
            await db.commit()
            await renew_next_plan_users(
                db, [user.id for user in users if user.next_plan is not None], lock_expirable, UserStatus.expired
            )


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


async def users_matching(db: AsyncSession, user_ids: list[int], predicates: tuple) -> list[User]:
    stmt = (
        _review_user_select_stmt()
        .where(User.id.in_(user_ids), *predicates)
        .order_by(User.id)
        .execution_options(populate_existing=True)
    )
    return list((await db.execute(stmt)).unique().scalars().all())


async def remove_on_hold_users_under_lock(user_ids: Iterable[int]) -> None:
    from app.jobs import review_users

    batches = _batches(user_ids, DELETE_CHUNK_SIZE)
    if not batches:
        return

    await start_on_hold_users_under_lock([user_id for batch in batches for user_id in batch], CAME_ONLINE)
    for batch in batches:
        async with GetDB() as db:
            users = await users_matching(db, batch, TIMED_OUT)
            if not users:
                await db.commit()
                continue
            responses = {
                user.id: await review_users.user_operator.validate_user(user, include_subscription_url=False)
                for user in users
            }
            await db.commit()

            for response in responses.values():
                await review_users.sync_remove_user_awaited(response)

            try:
                removed = await _delete_locked_users(db, list(responses), *TIMED_OUT)
                await db.commit()
            except Exception:
                await db.rollback()
                raise

            removed_ids = {row.id for row in removed}
            if removed:
                await sync_remove_users(removed)
            kept_ids = [user_id for user_id in responses if user_id not in removed_ids]
            if kept_ids:
                await review_users.sync_users(await users_matching(db, kept_ids, ()))

        for user_id in sorted(removed_ids):
            asyncio.create_task(review_users.notification.remove_user(responses[user_id], review_users.SYSTEM_ADMIN))
            review_users.logger.info(f'User "{responses[user_id].username}" removed after on-hold timeout')
