import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import SimpleNamespace

from PasarGuardNodeBridge import Health, PasarGuardNode
from PasarGuardNodeBridge.storage import InMemoryNodeLifecycleCoordinator, InMemoryUserSyncStore, LifecycleStatus
from sqlalchemy import delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

import app.node as node_package
from app.db import GetDB
from app.db.crud import node as node_crud
from app.db.models import Node, NodeConnectionType
from app.fork.models.node_retirement import NodeRetirement
from app.fork.operation.node_connect_jobs import readmit_connects, withdraw_connects
from app.fork.operation.node_extras import _connect_lock, ambiguous_start_pending, forget_connect_pushes
from app.fork.operation.node_messages import describe_error
from app.fork.operation.node_snapshots import clear_local_user_queue
from app.nats import needs_shared_bridge_memory
from app.nats.node_rpc import node_nats_client
from app.node import node_manager
from app.node.manager_sync import publish_node_sync
from app.node.nats_memory import clear_bridge_memory_for_node
from app.utils.logger import get_logger

logger = get_logger("node-retirement")

STOP_ATTEMPTS = 2
RETIRE_CONCURRENCY = 10
SETTLE_SECONDS = 300
RETRY_BASE_SECONDS = 60
RETRY_MAX_SECONDS = 3600
RETENTION = timedelta(days=7)
DRAIN_BATCH = 50
DRAIN_CONCURRENCY = 5
DRAIN_INTERVAL_SECONDS = 60

UNREACHABLE = "unreachable"
SETTLE = "settle"
HANDOFF = "handoff"
FIRST_ATTEMPT = {
    UNREACHABLE: timedelta(seconds=RETRY_BASE_SECONDS),
    SETTLE: timedelta(seconds=SETTLE_SECONDS),
    HANDOFF: timedelta(0),
}

type Sessions = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class Retirement(StrEnum):
    stopped = "stopped"
    unreachable = "unreachable"
    running = "running"
    handed_off = "handed_off"
    failed = "failed"
    absent = "absent"


REFUSED = frozenset({Retirement.running, Retirement.failed})
REFUSAL_REASONS = {
    Retirement.running: "is still running and did not stop",
    Retirement.failed: "could not be deleted",
}


@dataclass(frozen=True)
class RetireResult:
    node_id: int
    name: str
    outcome: Retirement
    detail: str = ""

    @property
    def refused(self) -> bool:
        return self.outcome in REFUSED


def _bridge_for(node) -> PasarGuardNode:
    return node_package.create_node(**node_manager._create_node_kwargs(node))


def _isolated_bridge_for(node) -> PasarGuardNode:
    kwargs = node_manager._create_node_kwargs(node)
    kwargs["user_sync_store"] = InMemoryUserSyncStore()
    kwargs["lifecycle_coordinator"] = InMemoryNodeLifecycleCoordinator()
    return node_package.create_node(**kwargs)


def rpc_timeout(node) -> int:
    return max(int(getattr(node, "internal_timeout", None) or 15), 1)


async def stop_and_verify(
    build: Callable[[], PasarGuardNode],
    timeout: int,
    reclaimed: Callable[[], Awaitable[bool]] | None = None,
) -> tuple[Retirement, str]:
    try:
        probe = await build().info(timeout=timeout)
    except Exception as exc:
        return Retirement.unreachable, describe_error(exc)
    if probe is None:
        return Retirement.unreachable, "the node returned no information"
    detail = ""
    for _ in range(STOP_ATTEMPTS):
        if reclaimed is not None and await reclaimed():
            return Retirement.absent, "the server is a node again"
        try:
            stopper = build()
            await stopper.set_health(Health.BROKEN)
            await stopper.stop(timeout=timeout)
        except Exception as exc:
            detail = f"Stop was not sent: {describe_error(exc)}"
        try:
            info = await build().info(timeout=timeout)
        except Exception as exc:
            return Retirement.unreachable, describe_error(exc)
        if info is None:
            return Retirement.unreachable, "the node returned no information"
        if not info.started:
            return Retirement.stopped, ""
        detail = detail or "the node still reports its core as started after Stop"
    return Retirement.running, detail


async def _restore_running_lifecycle(db_node) -> None:
    try:
        probe = _bridge_for(db_node)
        state = await probe.get_lifecycle_state()
        if state is not None and state.observed is not LifecycleStatus.HEALTHY:
            await probe.update_observed_lifecycle(LifecycleStatus.HEALTHY, expected_epoch=state.epoch)
    except Exception as exc:
        logger.warning(f'Could not record that the core of "{db_node.name}" is still running: {describe_error(exc)}')


def _owed_stop(db_node, reason: str, detail: str, now: datetime) -> dict:
    return {
        "node_id": db_node.id,
        "name": db_node.name,
        "connection_type": NodeConnectionType(db_node.connection_type).value,
        "address": db_node.address,
        "port": db_node.port,
        "api_port": db_node.api_port,
        "server_ca": db_node.server_ca,
        "api_key": db_node.api_key,
        "proxy_url": db_node.proxy_url or None,
        "default_timeout": int(db_node.default_timeout or 10),
        "internal_timeout": int(db_node.internal_timeout or 15),
        "reason": reason,
        "created_at": now,
        "next_attempt_at": now + FIRST_ATTEMPT[reason],
        "attempts": 0,
        "last_error": detail[:1024] or None,
    }


async def delete_node_rows(session: AsyncSession, db_node, owed: dict | None) -> None:
    async def owe_then_delete(ordered: list[int]) -> None:
        if owed is not None:
            await session.execute(insert(NodeRetirement.__table__).values(**owed))
        await node_crud._delete_node_rows(session, ordered)

    await node_crud.delete_nodes_in_lock_order(session, [db_node.id], owe_then_delete)


async def _row_exists(sessions: Sessions, node_id: int) -> bool:
    async with sessions() as session:
        return (await session.execute(select(Node.id).where(Node.id == node_id))).first() is not None


async def _clear_queued_work(node_id: int, name: str) -> None:
    try:
        await clear_bridge_memory_for_node(node_id)
        await clear_local_user_queue(node_id)
    except Exception as exc:
        logger.error(f'Deleted "{name}" but could not clear the user updates queued for it: {describe_error(exc)}')


async def _detach(node_id: int, name: str) -> None:
    await node_manager.remove_node(node_id, remote_stop=False)
    forget_connect_pushes(node_id)
    await _clear_queued_work(node_id, name)


def _owed_reason(node_id: int, outcome: Retirement, withdrew_a_start: bool) -> str | None:
    if outcome is Retirement.unreachable:
        return UNREACHABLE
    if withdrew_a_start or ambiguous_start_pending(node_id) or needs_shared_bridge_memory():
        return SETTLE
    return None


async def retire_here(db_node, sessions: Sessions, row_deletes: asyncio.Lock | None = None) -> RetireResult:
    try:
        withdrew_a_start = await withdraw_connects(db_node.id)
        async with _connect_lock(db_node.id):
            if not await _row_exists(sessions, db_node.id):
                return RetireResult(db_node.id, db_node.name, Retirement.absent, "")
            outcome, detail = await stop_and_verify(lambda: _bridge_for(db_node), rpc_timeout(db_node))
            if outcome is Retirement.running:
                await _restore_running_lifecycle(db_node)
                logger.warning(f'Not deleting "{db_node.name}": its core is still running after Stop: {detail}')
                return RetireResult(db_node.id, db_node.name, outcome, detail)
            reason = _owed_reason(db_node.id, outcome, withdrew_a_start)
            owed = None if reason is None else _owed_stop(db_node, reason, detail, datetime.now(UTC))
            async with row_deletes or contextlib.nullcontext(), sessions() as session:
                await delete_node_rows(session, db_node, owed)
            await _detach(db_node.id, db_node.name)
    finally:
        readmit_connects(db_node.id)
    if needs_shared_bridge_memory():
        await publish_node_sync("remove", db_node.id)
    if outcome is Retirement.unreachable:
        logger.warning(
            f'Deleted "{db_node.name}" without confirming its core stopped ({detail}); '
            "its Stop is retried until the node answers or the retention ends"
        )
    return RetireResult(db_node.id, db_node.name, outcome, detail)


async def hand_off(db_node, sessions: Sessions, row_deletes: asyncio.Lock | None = None) -> RetireResult:
    owed = _owed_stop(db_node, HANDOFF, "", datetime.now(UTC))
    async with row_deletes or contextlib.nullcontext(), sessions() as session:
        await delete_node_rows(session, db_node, owed)
    await node_nats_client.publish("remove_node", {"node_id": db_node.id})
    return RetireResult(db_node.id, db_node.name, Retirement.handed_off, "")


async def retire_nodes(db: AsyncSession, db_nodes: Sequence[Node], *, here: bool) -> list[RetireResult]:
    gate = asyncio.Semaphore(RETIRE_CONCURRENCY)
    row_deletes = asyncio.Lock()

    def sessions() -> AsyncSession:
        return AsyncSession(bind=db.bind, expire_on_commit=False)

    async def retire(db_node) -> RetireResult:
        async with gate:
            try:
                if here:
                    return await retire_here(db_node, sessions, row_deletes)
                return await hand_off(db_node, sessions, row_deletes)
            except Exception as exc:
                detail = describe_error(exc)
                logger.error(f'Deleting "{db_node.name}" failed: {detail}')
                return RetireResult(db_node.id, db_node.name, Retirement.failed, detail)

    return list(await asyncio.gather(*(retire(db_node) for db_node in db_nodes)))


def refusal_message(results: Sequence[RetireResult]) -> str | None:
    refused = [result for result in results if result.refused]
    if not refused:
        return None
    reasons = "; ".join(
        f'"{result.name}" {REFUSAL_REASONS[result.outcome]}' + (f" ({result.detail})" if result.detail else "")
        for result in refused
    )
    deleted = [f'"{result.name}"' for result in results if not result.refused]
    done = f"Deleted {', '.join(deleted)}. " if deleted else ""
    return (
        f"{done}Kept and still managed: {reasons}. Try again, or stop the node service on that server first: "
        "a node that does not answer at all is deleted, and its Stop is retried until it answers."
    )


def _retired_node(row: NodeRetirement) -> SimpleNamespace:
    return SimpleNamespace(
        id=row.node_id,
        name=row.name,
        connection_type=NodeConnectionType(row.connection_type),
        address=row.address,
        port=row.port,
        api_port=row.api_port,
        server_ca=row.server_ca,
        api_key=row.api_key,
        proxy_url=row.proxy_url,
        default_timeout=row.default_timeout,
        internal_timeout=row.internal_timeout,
        usage_coefficient=1.0,
    )


def _as_utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * 2 ** max(attempts - 1, 0)))


async def _claim(row: NodeRetirement, now: datetime) -> bool:
    attempts = row.attempts + 1
    async with GetDB() as db:
        claimed = await db.execute(
            update(NodeRetirement)
            .where(NodeRetirement.id == row.id, NodeRetirement.attempts == row.attempts)
            .values(attempts=attempts, next_attempt_at=now + _backoff(attempts))
        )
        await db.commit()
    return claimed.rowcount == 1


async def _forget(row: NodeRetirement) -> None:
    async with GetDB() as db:
        await db.execute(delete(NodeRetirement).where(NodeRetirement.id == row.id))
        await db.commit()


async def _record_failure(row: NodeRetirement, detail: str) -> None:
    async with GetDB() as db:
        await db.execute(update(NodeRetirement).where(NodeRetirement.id == row.id).values(last_error=detail[:1024]))
        await db.commit()


async def _recheck_at(row: NodeRetirement, moment: datetime) -> None:
    async with GetDB() as db:
        await db.execute(update(NodeRetirement).where(NodeRetirement.id == row.id).values(next_attempt_at=moment))
        await db.commit()


async def _server_is_a_node_again(row: NodeRetirement) -> bool:
    async with GetDB() as db:
        found = await db.execute(select(Node.id).where(Node.address == row.address, Node.port == row.port).limit(1))
        return found.first() is not None


async def _detach_if_still_managed(row: NodeRetirement) -> None:
    if await node_manager.get_node(row.node_id) is None:
        return
    async with GetDB() as db:
        found = await db.execute(select(Node.id).where(Node.id == row.node_id))
        if found.first() is not None:
            return
    await _detach(row.node_id, row.name)
    logger.info(f'Stopped managing deleted node "{row.name}", whose removal never reached this process')


async def _settle(row: NodeRetirement, now: datetime) -> None:
    if not await _claim(row, now):
        return
    await _detach_if_still_managed(row)
    if await _server_is_a_node_again(row):
        await _forget(row)
        logger.info(f'Dropped the owed Stop of deleted node "{row.name}": {row.address}:{row.port} is a node again')
        return
    outcome, detail = await stop_and_verify(
        lambda: _isolated_bridge_for(_retired_node(row)),
        rpc_timeout(row),
        reclaimed=lambda: _server_is_a_node_again(row),
    )
    if outcome is Retirement.absent:
        await _forget(row)
        logger.info(f'Dropped the owed Stop of deleted node "{row.name}": {row.address}:{row.port} is a node again')
        return
    if outcome is Retirement.stopped:
        settled_at = _as_utc(row.created_at) + timedelta(seconds=SETTLE_SECONDS)
        if row.reason == HANDOFF and now < settled_at:
            await _recheck_at(row, settled_at)
            return
        await _forget(row)
        logger.info(f'The core of deleted node "{row.name}" at {row.address} is confirmed stopped')
        return
    if now - _as_utc(row.created_at) >= RETENTION:
        await _forget(row)
        logger.error(
            f'Gave up stopping the core of deleted node "{row.name}" at {row.address}:{row.port} after '
            f"{row.attempts + 1} attempts ({detail}); it may still be serving its last user list"
        )
        return
    await _record_failure(row, detail)
    logger.warning(f'The core of deleted node "{row.name}" at {row.address} is not confirmed stopped yet: {detail}')


async def drain_node_retirements(now: datetime | None = None) -> None:
    moment = now or datetime.now(UTC)
    async with GetDB() as db:
        rows = (
            (
                await db.execute(
                    select(NodeRetirement)
                    .where(NodeRetirement.next_attempt_at <= moment)
                    .order_by(NodeRetirement.next_attempt_at)
                    .limit(DRAIN_BATCH)
                )
            )
            .scalars()
            .all()
        )
    gate = asyncio.Semaphore(DRAIN_CONCURRENCY)

    async def settle(row: NodeRetirement) -> None:
        async with gate:
            try:
                await _settle(row, moment)
            except Exception as exc:
                logger.error(f'Retrying the Stop owed to deleted node "{row.name}" failed: {describe_error(exc)}')

    await asyncio.gather(*(settle(row) for row in rows))
