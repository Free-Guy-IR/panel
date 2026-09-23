from __future__ import annotations

import asyncio
from collections.abc import Sequence

from sqlalchemy.exc import DBAPIError

from app.db import AsyncSession, GetDB
from app.db.crud.user import get_users_by_ids
from app.db.crud.wireguard import sync_users_allocations
from app.db.models import User
from app.fork.proxy_secrets.entitled import grant_entitled_secrets
from app.fork.proxy_secrets.locks import (
    MYSQL_FAMILY,
    NamedLockMissing,
    acquire_proxy_secrets_lock,
    is_deadlock,
    is_lock_unavailable,
)
from app.lifecycle import on_shutdown
from app.node.sync import sync_users
from app.utils.logger import get_logger

logger = get_logger("group-allocation-repair")

IN_REQUEST_RETRY_PAUSE_SECONDS = 1.0
REPAIR_DELAYS_SECONDS: tuple[float, ...] = (5.0, 30.0, 120.0)
LOCK_FAILURES = (DBAPIError, NamedLockMissing)

_pending_repairs: dict[asyncio.Task, list[int]] = {}
_queued_user_ids: set[int] = set()


async def allocate_and_grant(db: AsyncSession, users: Sequence[User]) -> None:
    await sync_users_allocations(db, list(users))
    await grant_entitled_secrets(db, list(users))


def _whole_transaction_is_gone(db: AsyncSession, exc: BaseException) -> bool:
    return db.get_bind().dialect.name in MYSQL_FAMILY and is_deadlock(exc)


async def _attempt(db: AsyncSession, users: Sequence[User]) -> tuple[BaseException | None, bool]:
    if db.get_bind().dialect.name == "sqlite":
        try:
            await acquire_proxy_secrets_lock(db)
        except LOCK_FAILURES as exc:
            if not is_lock_unavailable(exc):
                raise
            return exc, True
    savepoint = await db.begin_nested()
    try:
        await allocate_and_grant(db, users)
    except LOCK_FAILURES as exc:
        if not is_lock_unavailable(exc):
            raise
        try:
            await savepoint.rollback()
        except DBAPIError:
            return exc, False
        return exc, not _whole_transaction_is_gone(db, exc)
    await savepoint.commit()
    return None, True


async def allocate_and_grant_with_one_retry(db: AsyncSession, users: Sequence[User], user_ids: Sequence[int]) -> None:
    failure, retryable = await _attempt(db, users)
    if failure is None:
        return
    if not retryable:
        raise failure
    logger.warning(
        f"WireGuard allocation and entitled secrets for users {list(user_ids)[:20]} could not take the proxy "
        f"secrets lock ({failure}); retrying once in {IN_REQUEST_RETRY_PAUSE_SECONDS:g}s"
    )
    await asyncio.sleep(IN_REQUEST_RETRY_PAUSE_SECONDS)
    await get_users_by_ids(db, list(user_ids), load_admin_role=True)
    failure, _ = await _attempt(db, users)
    if failure is not None:
        raise failure


async def repair_allocations(user_ids: Sequence[int]) -> bool:
    user_ids = sorted(set(user_ids))
    attempts = len(REPAIR_DELAYS_SECONDS)
    for attempt, delay in enumerate(REPAIR_DELAYS_SECONDS, start=1):
        await asyncio.sleep(delay)
        try:
            async with GetDB() as db:
                users = await get_users_by_ids(db, user_ids, load_admin_role=True)
                await allocate_and_grant(db, users)
                await db.commit()
                await sync_users(users)
        except Exception as exc:
            logger.warning(
                f"attempt {attempt}/{attempts} to finish WireGuard allocation and entitled secrets for users "
                f"{user_ids[:20]} after a group change failed: {exc}"
            )
            continue
        logger.info(f"finished WireGuard allocation and entitled secrets for {len(user_ids)} user(s) in the background")
        return True
    logger.error(
        f"gave up finishing WireGuard allocation and entitled secrets for users {user_ids[:20]} after {attempts} "
        "attempts; nothing of that step was stored, run the protocol activation or repeat the group change"
    )
    return False


def _forget(task: asyncio.Task) -> None:
    _queued_user_ids.difference_update(_pending_repairs.pop(task, []))


def schedule_allocation_repair(user_ids: Sequence[int]) -> asyncio.Task | None:
    fresh = sorted(set(user_ids) - _queued_user_ids)
    if not fresh:
        return None
    _queued_user_ids.update(fresh)
    task = asyncio.create_task(repair_allocations(fresh))
    _pending_repairs[task] = fresh
    task.add_done_callback(_forget)
    return task


def pending_repair_user_ids() -> set[int]:
    return set(_queued_user_ids)


async def cancel_pending_repairs() -> None:
    tasks = [task for task in _pending_repairs if not task.done()]
    for task in tasks:
        logger.warning(
            f"shutting down before the background WireGuard allocation and entitled secrets for users "
            f"{_pending_repairs.get(task, [])[:20]} finished; run the protocol activation or repeat the group change"
        )
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


on_shutdown(cancel_pending_repairs)
