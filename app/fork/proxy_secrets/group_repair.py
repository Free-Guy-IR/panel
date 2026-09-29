from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import anyio
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import joinedload, selectinload

from app.db import AsyncSession, GetDB
from app.db.crud.user import get_users_by_ids
from app.db.crud.wireguard import sync_users_allocations
from app.db.models import Admin, Group, User
from app.fork.proxy_secrets import resync_markers
from app.fork.proxy_secrets.entitled import grant_entitled_secrets
from app.fork.proxy_secrets.errors import describe_failure
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
IN_REQUEST_LOCK_WAIT_SECONDS = 10
REPAIR_DELAYS_SECONDS: tuple[float, ...] = (5.0, 30.0, 120.0)
REPAIR_MAX_PASSES = 5
PUSH_LOAD_CHUNK = 1000
DRAIN_MIN_AGE_SECONDS = 600.0
DRAIN_CHUNK = 200
LOCK_FAILURES = (DBAPIError, NamedLockMissing)
LOCK_WAIT_VARIABLE = "innodb_lock_wait_timeout"

_pending_repairs: dict[asyncio.Task, list[int]] = {}
_repair_owners: dict[int, asyncio.Task] = {}
_dirty_user_ids: set[int] = set()


PushUsers = Callable[[list[User]], Awaitable[None]]


async def allocate_and_grant(db: AsyncSession, users: Sequence[User]) -> None:
    await acquire_proxy_secrets_lock(db)
    await sync_users_allocations(db, list(users))
    await grant_entitled_secrets(db, list(users))


def _committed_users_statement(user_ids: Sequence[int]):
    return (
        select(User)
        .where(User.id.in_(user_ids))
        .order_by(User.id)
        .options(
            joinedload(User.admin).selectinload(Admin.role),
            selectinload(User.groups).selectinload(Group.inbounds),
        )
    )


async def push_committed_users(db: AsyncSession, user_ids: Iterable[int], push: PushUsers) -> None:
    ordered = sorted(set(user_ids))
    if not ordered:
        return
    async with AsyncSession(db.bind, autoflush=False, expire_on_commit=False) as fresh:
        users: list[User] = []
        for start in range(0, len(ordered), PUSH_LOAD_CHUNK):
            chunk = ordered[start : start + PUSH_LOAD_CHUNK]
            users.extend((await fresh.execute(_committed_users_statement(chunk))).unique().scalars().all())
        await push(users)


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


async def _driver_scalar(driver_connection, sql: str, args=None):
    async with driver_connection.cursor() as cursor:
        await cursor.execute(sql, args)
        row = await cursor.fetchone()
    return row[0] if row else None


async def _restore_lock_wait(connection, driver_connection, previous) -> None:
    with anyio.CancelScope(shield=True):
        try:
            await _driver_scalar(driver_connection, f"SET SESSION {LOCK_WAIT_VARIABLE} = %s", (previous,))
        except BaseException as exc:
            logger.warning(
                f"could not restore {LOCK_WAIT_VARIABLE} on a pooled connection, discarding it: "
                f"{describe_failure(exc)}"
            )
            await connection.invalidate()
            raise


@asynccontextmanager
async def _bounded_lock_wait(db: AsyncSession) -> AsyncIterator[None]:
    if db.get_bind().dialect.name not in MYSQL_FAMILY:
        yield
        return
    connection = await db.connection()
    driver_connection = (await connection.get_raw_connection()).driver_connection
    previous = await _driver_scalar(driver_connection, f"SELECT @@SESSION.{LOCK_WAIT_VARIABLE}")
    try:
        await _driver_scalar(
            driver_connection, f"SET SESSION {LOCK_WAIT_VARIABLE} = %s", (IN_REQUEST_LOCK_WAIT_SECONDS,)
        )
        yield
    finally:
        await _restore_lock_wait(connection, driver_connection, previous)


async def _attempt_twice(db: AsyncSession, users: Sequence[User], user_ids: Sequence[int]) -> None:
    failure, retryable = await _attempt(db, users)
    if failure is None:
        return
    if not retryable:
        raise failure
    logger.warning(
        f"WireGuard allocation and entitled secrets for users {list(user_ids)[:20]} could not take the proxy "
        f"secrets lock ({describe_failure(failure)}); retrying once in {IN_REQUEST_RETRY_PAUSE_SECONDS:g}s"
    )
    await asyncio.sleep(IN_REQUEST_RETRY_PAUSE_SECONDS)
    await acquire_proxy_secrets_lock(db)
    await get_users_by_ids(db, list(user_ids), load_admin_role=True)
    failure, _ = await _attempt(db, users)
    if failure is not None:
        raise failure


async def allocate_and_grant_with_one_retry(db: AsyncSession, users: Sequence[User], user_ids: Sequence[int]) -> None:
    await db.commit()
    async with _bounded_lock_wait(db):
        await _attempt_twice(db, users, user_ids)


def _retry_keeps_the_transaction(db: AsyncSession, exc: BaseException) -> bool:
    return isinstance(exc, NamedLockMissing) or db.get_bind().dialect.name != "postgresql"


async def _lock_with_one_retry(db: AsyncSession) -> None:
    try:
        await acquire_proxy_secrets_lock(db)
        return
    except LOCK_FAILURES as exc:
        if not is_lock_unavailable(exc) or not _retry_keeps_the_transaction(db, exc):
            raise
        failure = exc
    logger.warning(
        f"a group change could not take the proxy secrets lock before writing its resync markers "
        f"({describe_failure(failure)}); retrying once in {IN_REQUEST_RETRY_PAUSE_SECONDS:g}s"
    )
    await asyncio.sleep(IN_REQUEST_RETRY_PAUSE_SECONDS)
    await acquire_proxy_secrets_lock(db)


async def mark_under_the_lock(db: AsyncSession, mark: Callable[..., Awaitable[None]], *args) -> None:
    async with _bounded_lock_wait(db):
        await _lock_with_one_retry(db)
        await mark(db, *args)


async def pending_resync_markers(db: AsyncSession, user_ids: Iterable[int]) -> list[resync_markers.Marker]:
    return await resync_markers.markers_for(db, user_ids)


async def clear_resync_markers(db: AsyncSession, markers: Iterable[resync_markers.Marker]) -> None:
    await resync_markers.clear_markers(db, markers)
    await db.commit()


async def finish_in_background(user_ids: list[int], markers: Iterable[resync_markers.Marker] | None = None) -> None:
    async with GetDB() as db:
        users = await get_users_by_ids(db, user_ids, load_admin_role=True)
        if markers is None:
            markers = await resync_markers.markers_for(db, user_ids)
        await db.commit()
        await allocate_and_grant(db, users)
        await db.commit()
        await push_committed_users(db, user_ids, sync_users)
        await clear_resync_markers(db, markers)


async def _repair_pass(user_ids: list[int], pass_number: int) -> bool:
    attempts = len(REPAIR_DELAYS_SECONDS)
    for attempt, delay in enumerate(REPAIR_DELAYS_SECONDS, start=1):
        await asyncio.sleep(delay)
        try:
            await finish_in_background(user_ids)
        except Exception as exc:
            logger.warning(
                f"pass {pass_number}, attempt {attempt}/{attempts} to finish WireGuard allocation and entitled "
                f"secrets for users {user_ids[:20]} after a group change failed: {describe_failure(exc)}"
            )
            continue
        logger.info(
            f"finished WireGuard allocation and entitled secrets for {len(user_ids)} user(s) in the background "
            f"(pass {pass_number})"
        )
        return True
    logger.error(
        f"gave up finishing WireGuard allocation and entitled secrets for users {user_ids[:20]} after {attempts} "
        "attempts; nothing of that step was stored, and the pending-resync job retries it from the durable marker"
    )
    return False


async def drain_pending_resyncs(*, min_age_seconds: float = DRAIN_MIN_AGE_SECONDS) -> int:
    requested_before = datetime.now(UTC) - timedelta(seconds=min_age_seconds)
    drained = 0
    after_user_id = 0
    while True:
        async with GetDB() as db:
            markers = await resync_markers.due_markers(
                db, requested_before, DRAIN_CHUNK, after_user_id=after_user_id
            )
        if not markers:
            return drained
        user_ids = sorted({user_id for user_id, _ in markers})
        after_user_id = user_ids[-1]
        try:
            await finish_in_background(user_ids, markers)
        except Exception as exc:
            logger.warning(
                f"could not finish the pending WireGuard allocation and entitled secrets of users {user_ids[:20]} "
                f"left by an earlier group change; the next run retries: {describe_failure(exc)}"
            )
            continue
        drained += len(user_ids)


async def repair_allocations(user_ids: Sequence[int]) -> bool:
    owned = sorted(set(user_ids))
    pending = owned
    try:
        for pass_number in range(1, REPAIR_MAX_PASSES + 1):
            _dirty_user_ids.difference_update(pending)
            if not await _repair_pass(pending, pass_number):
                return False
            pending = sorted(_dirty_user_ids.intersection(owned))
            if not pending:
                return True
        logger.error(
            f"users {pending[:20]} kept changing while their background WireGuard allocation and entitled secrets "
            f"repair ran; stopped after {REPAIR_MAX_PASSES} passes, so their latest group change may still lack its "
            "addresses or secrets until the pending-resync job finishes them from the durable marker"
        )
        return False
    finally:
        _release(asyncio.current_task(), owned)


def _release(task: asyncio.Task | None, user_ids: Sequence[int]) -> None:
    for user_id in user_ids:
        if _repair_owners.get(user_id) is task:
            del _repair_owners[user_id]
            _dirty_user_ids.discard(user_id)


def _forget(task: asyncio.Task) -> None:
    _release(task, _pending_repairs.pop(task, []))


def schedule_allocation_repair(user_ids: Sequence[int]) -> asyncio.Task | None:
    wanted = set(user_ids)
    _dirty_user_ids.update(wanted.intersection(_repair_owners))
    fresh = sorted(wanted.difference(_repair_owners))
    if not fresh:
        return None
    task = asyncio.create_task(repair_allocations(fresh))
    for user_id in fresh:
        _repair_owners[user_id] = task
    _pending_repairs[task] = fresh
    task.add_done_callback(_forget)
    return task


def pending_repair_user_ids() -> set[int]:
    return set(_repair_owners)


def dirty_repair_user_ids() -> set[int]:
    return set(_dirty_user_ids)


async def cancel_pending_repairs() -> None:
    tasks = [task for task in _pending_repairs if not task.done()]
    for task in tasks:
        logger.warning(
            f"shutting down before the background WireGuard allocation and entitled secrets for users "
            f"{_pending_repairs.get(task, [])[:20]} finished; the pending-resync job finishes them after the restart"
        )
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


on_shutdown(cancel_pending_repairs)
