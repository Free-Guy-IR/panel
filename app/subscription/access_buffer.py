from __future__ import annotations

import asyncio
from datetime import UTC, datetime as dt
from time import monotonic
from typing import Any

from sqlalchemy import insert, select

from app.db import GetDB
from app.db.models import SubscriptionAccessKind, User, UserSubscriptionAccess
from app.lifecycle import on_shutdown, on_startup
from app.utils.logger import get_logger
from config import runtime_settings, subscription_env_settings

logger = get_logger("sub-access-buffer")

FLUSH_INTERVAL_SECONDS = 5.0
FLUSH_BATCH_SIZE = 100
_MAX_BUFFER = 20_000
_MAX_SEEN_ENTRIES = 20_000

_KIND_MAX_LEN = UserSubscriptionAccess.__table__.columns.access_kind.type.length or 32
_USER_AGENT_MAX_LEN = UserSubscriptionAccess.__table__.columns.user_agent.type.length or 512
_IP_MAX_LEN = UserSubscriptionAccess.__table__.columns.ip.type.length or 64
_SEEN_AGENT_KEY_LEN = 128

_pending: list[dict[str, Any]] = []
_seen: dict[tuple[int, str, str], float] = {}
_lock = asyncio.Lock()
_drain_lock = asyncio.Lock()
_flush_task: asyncio.Task | None = None


def _sanitize_record(user_id: int, kind: str, user_agent: str | None, ip: str | None) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "access_kind": kind[:_KIND_MAX_LEN],
        "user_agent": (user_agent or "")[:_USER_AGENT_MAX_LEN] or None,
        "ip": (ip or "")[:_IP_MAX_LEN] or None,
        "created_at": dt.now(UTC),
    }


def _seen_key(user_id: int, kind: str, user_agent: str | None) -> tuple[int, str, str]:
    return (user_id, kind, (user_agent or "")[:_SEEN_AGENT_KEY_LEN])


def _claim_slot(user_id: int, kind: str, user_agent: str | None) -> bool:
    window = subscription_env_settings.access_dedupe_seconds
    if window <= 0:
        return True

    key = _seen_key(user_id, kind, user_agent)
    now = monotonic()
    seen_at = _seen.get(key)
    if seen_at is not None and now - seen_at < window:
        return False

    if len(_seen) >= _MAX_SEEN_ENTRIES:
        for stale_key, stale_at in list(_seen.items()):
            if now - stale_at >= window:
                _seen.pop(stale_key, None)
        if len(_seen) >= _MAX_SEEN_ENTRIES:
            _seen.clear()

    _seen[key] = now
    return True


def pending_count() -> int:
    return len(_pending)


async def reset_subscription_access_buffer() -> None:
    async with _lock:
        _pending.clear()
        _seen.clear()


async def queue_subscription_access(
    user_id: int,
    kind: SubscriptionAccessKind | str,
    user_agent: str | None = None,
    ip: str | None = None,
) -> None:
    if subscription_env_settings.access_limit <= 0:
        return

    kind_value = kind.value if isinstance(kind, SubscriptionAccessKind) else str(kind)

    should_flush = False
    dropped = 0
    async with _lock:
        if not _claim_slot(user_id, kind_value, user_agent):
            return
        prev = len(_pending)
        _pending.append(_sanitize_record(user_id, kind_value, user_agent, ip))
        overflow = len(_pending) - _MAX_BUFFER
        if overflow > 0:
            del _pending[:overflow]
            dropped = overflow
        should_flush = prev < FLUSH_BATCH_SIZE <= len(_pending)
    if dropped:
        logger.warning("Dropped %s buffered subscription accesses; buffer full", dropped)
    if should_flush:
        asyncio.create_task(flush_subscription_accesses(), name="sub_access_flush")


async def flush_subscription_accesses() -> int:
    async with _drain_lock:
        written = 0
        while True:
            async with _lock:
                if not _pending:
                    return written
                batch = _pending[:FLUSH_BATCH_SIZE]
                del _pending[:FLUSH_BATCH_SIZE]
            try:
                async with GetDB() as db:
                    user_ids = sorted({record["user_id"] for record in batch})
                    existing_user_ids = set(
                        await db.scalars(
                            select(User.id)
                            .where(User.id.in_(user_ids))
                            .order_by(User.id)
                            .with_for_update(read=True, key_share=True)
                        )
                    )
                    live_records = [record for record in batch if record["user_id"] in existing_user_ids]
                    if live_records:
                        await db.execute(insert(UserSubscriptionAccess.__table__), live_records)
                    await db.commit()
                written += len(live_records)
            except Exception:
                async with _lock:
                    _pending[0:0] = batch
                    overflow = len(_pending) - _MAX_BUFFER
                    if overflow > 0:
                        del _pending[:overflow]
                logger.exception("Failed to flush %s buffered subscription accesses", len(batch))
                raise


async def _flush_loop() -> None:
    while True:
        await asyncio.sleep(FLUSH_INTERVAL_SECONDS)
        try:
            await flush_subscription_accesses()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Periodic subscription-access flush failed")


@on_startup
async def start_subscription_access_flusher() -> None:
    global _flush_task
    if not runtime_settings.role.runs_panel or subscription_env_settings.access_limit <= 0:
        return
    if _flush_task is not None and not _flush_task.done():
        return
    _flush_task = asyncio.create_task(_flush_loop(), name="sub_access_flush_loop")


@on_shutdown
async def stop_subscription_access_flusher() -> None:
    global _flush_task
    if _flush_task is not None:
        _flush_task.cancel()
        try:
            await _flush_task
        except asyncio.CancelledError:
            pass
        _flush_task = None
    try:
        await flush_subscription_accesses()
    except Exception:
        logger.exception("Final subscription-access flush failed")
