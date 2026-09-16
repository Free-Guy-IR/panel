import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User
from app.fork.models.traffic_log import TrafficLogIdentity, TrafficLogRecord

TTL_SECONDS = 60.0
QUERY_CHUNK = 500
RECONCILE_CHUNK = 500
RECONCILE_MAX = 50_000
CACHE_CAP = 50_000
CACHE_MAX_AGE_SECONDS = 3_600.0


@dataclass(slots=True)
class IdentityEntry:
    username: str | None
    admin_id: int | None
    deleted: bool
    resolved_at: float


def _chunks(values: list[int], size: int) -> Iterable[list[int]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


class IdentityCache:
    def __init__(self, ttl_seconds: float = TTL_SECONDS):
        self._entries: dict[int, IdentityEntry] = {}
        self._ttl = ttl_seconds

    def is_resolved(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self._entries

    def username_of(self, user_id: int | None) -> str | None:
        entry = self._entries.get(user_id) if user_id is not None else None
        return entry.username if entry else None

    def owner_of(self, user_id: int | None) -> int | None:
        entry = self._entries.get(user_id) if user_id is not None else None
        return entry.admin_id if entry else None

    def fresh_owner_of(self, user_id: int | None) -> int | None:
        entry = self._entries.get(user_id) if user_id is not None else None
        if entry is None or time.monotonic() - entry.resolved_at >= self._ttl:
            return None
        return entry.admin_id

    def is_deleted(self, user_id: int | None) -> bool:
        entry = self._entries.get(user_id) if user_id is not None else None
        return bool(entry and entry.deleted)

    def stale(self, ids: Iterable[int]) -> set[int]:
        now = time.monotonic()
        result: set[int] = set()
        for user_id in ids:
            entry = self._entries.get(user_id)
            if entry is None or now - entry.resolved_at >= self._ttl:
                result.add(user_id)
        return result

    def __len__(self) -> int:
        return len(self._entries)

    def prune(self, cap: int = CACHE_CAP, max_age: float = CACHE_MAX_AGE_SECONDS) -> int:
        now = time.monotonic()
        dropped = 0
        for user_id in [uid for uid, entry in self._entries.items() if now - entry.resolved_at >= max_age]:
            self._entries.pop(user_id, None)
            dropped += 1
        excess = len(self._entries) - cap
        if excess > 0:
            ordered = sorted(self._entries.items(), key=lambda item: item[1].resolved_at)
            for user_id, _ in ordered[:excess]:
                self._entries.pop(user_id, None)
            dropped += excess
        return dropped

    async def resolve_many(self, db: AsyncSession, ids: Iterable[int]) -> None:
        wanted = sorted({int(user_id) for user_id in ids})
        if not wanted:
            return
        now = datetime.now(UTC)
        changed = False
        for chunk in _chunks(wanted, QUERY_CHUNK):
            changed |= await self._resolve_chunk(db, chunk, now)
        if changed:
            await db.commit()

    async def _resolve_chunk(self, db: AsyncSession, chunk: list[int], now: datetime) -> bool:
        live = {
            row.id: (row.username, row.admin_id)
            for row in (await db.execute(select(User.id, User.username, User.admin_id).where(User.id.in_(chunk)))).all()
        }
        stored = {
            row.user_id: (row.username, row.admin_id, bool(row.deleted))
            for row in (
                await db.execute(
                    select(
                        TrafficLogIdentity.user_id,
                        TrafficLogIdentity.username,
                        TrafficLogIdentity.admin_id,
                        TrafficLogIdentity.deleted,
                    ).where(TrafficLogIdentity.user_id.in_(chunk))
                )
            ).all()
        }
        resolved_at = time.monotonic()
        changed = False
        for user_id in chunk:
            previous = stored.get(user_id)
            if user_id in live:
                username, admin_id = live[user_id]
                deleted = False
            else:
                username = previous[0] if previous else None
                admin_id = previous[1] if previous else None
                deleted = True
            self._entries[user_id] = IdentityEntry(username, admin_id, deleted, resolved_at)
            if username is None:
                continue
            values = {"username": username, "admin_id": admin_id, "deleted": deleted, "updated_at": now}
            if previous is None:
                await db.execute(insert(TrafficLogIdentity).values(user_id=user_id, **values))
                changed = True
            elif previous != (username, admin_id, deleted):
                await db.execute(
                    update(TrafficLogIdentity).where(TrafficLogIdentity.user_id == user_id).values(**values)
                )
                changed = True
        return changed


async def forget_unreferenced_identities(db: AsyncSession) -> int:
    referenced = (
        select(TrafficLogRecord.id)
        .where(TrafficLogRecord.user_id == TrafficLogIdentity.user_id)
        .correlate(TrafficLogIdentity)
        .exists()
    )
    removed = 0
    while removed < RECONCILE_MAX:
        ids = (
            (
                await db.execute(
                    select(TrafficLogIdentity.user_id).where(~referenced).limit(RECONCILE_CHUNK)
                )
            )
            .scalars()
            .all()
        )
        if not ids:
            break
        await db.execute(delete(TrafficLogIdentity).where(TrafficLogIdentity.user_id.in_(ids)))
        await db.commit()
        removed += len(ids)
        if len(ids) < RECONCILE_CHUNK:
            break
    return removed


async def reconcile_identities(db: AsyncSession) -> int:
    now = datetime.now(UTC)
    referenced = (
        select(TrafficLogRecord.id)
        .where(TrafficLogRecord.user_id == TrafficLogIdentity.user_id)
        .correlate(TrafficLogIdentity)
        .exists()
    )
    reconciled = 0
    scanned = 0
    after = -1
    while scanned < RECONCILE_MAX:
        rows = (
            await db.execute(
                select(
                    TrafficLogIdentity.user_id,
                    TrafficLogIdentity.username,
                    TrafficLogIdentity.admin_id,
                    TrafficLogIdentity.deleted,
                )
                .where(TrafficLogIdentity.user_id > after, referenced)
                .order_by(TrafficLogIdentity.user_id)
                .limit(RECONCILE_CHUNK)
            )
        ).all()
        if not rows:
            break
        after = int(rows[-1][0])
        scanned += len(rows)
        live = {
            row.id: (row.username, row.admin_id)
            for row in (
                await db.execute(
                    select(User.id, User.username, User.admin_id).where(User.id.in_([int(row[0]) for row in rows]))
                )
            ).all()
        }
        pending = False
        for user_id, username, admin_id, deleted in rows:
            current = live.get(int(user_id))
            if current is None:
                if deleted:
                    continue
                values = {"deleted": True, "updated_at": now}
            else:
                live_username, live_admin_id = current
                if not deleted and username == live_username and admin_id == live_admin_id:
                    continue
                values = {
                    "username": live_username,
                    "admin_id": live_admin_id,
                    "deleted": False,
                    "updated_at": now,
                }
            await db.execute(update(TrafficLogIdentity).where(TrafficLogIdentity.user_id == user_id).values(**values))
            reconciled += 1
            pending = True
        if pending:
            await db.commit()
        if len(rows) < RECONCILE_CHUNK:
            break
    return reconciled
