from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import delete, select, tuple_
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User
from app.fork.models.pending_node_removal import PendingNodeRemoval
from app.fork.proxy_secrets.locks import MYSQL_FAMILY

REMOVAL_CHUNK = 1000

Removal = tuple[int, str]


def _chunks(values: Sequence, size: int = REMOVAL_CHUNK) -> Iterable[Sequence]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _upsert(dialect_name: str, rows: list[dict]):
    table = PendingNodeRemoval.__table__
    if dialect_name in MYSQL_FAMILY:
        statement = mysql_insert(table).values(rows)
        return statement.on_duplicate_key_update(
            token=statement.inserted.token, requested_at=statement.inserted.requested_at
        )
    insert = pg_insert if dialect_name == "postgresql" else sqlite_insert
    statement = insert(table).values(rows)
    return statement.on_conflict_do_update(
        index_elements=["user_id"],
        set_={"token": statement.excluded.token, "requested_at": statement.excluded.requested_at},
    )


async def mark_node_removals(db: AsyncSession, user_ids: Iterable[int]) -> None:
    ordered = sorted(set(user_ids))
    if not ordered:
        return
    token = uuid4().hex
    requested_at = datetime.now(UTC)
    dialect_name = db.get_bind().dialect.name
    for chunk in _chunks(ordered):
        rows = [{"user_id": user_id, "token": token, "requested_at": requested_at} for user_id in chunk]
        await db.execute(_upsert(dialect_name, rows))


async def due_node_removals(db: AsyncSession, limit: int, *, after_user_id: int = 0) -> list[Removal]:
    table = PendingNodeRemoval.__table__
    rows = await db.execute(
        select(table.c.user_id, table.c.token)
        .where(table.c.user_id > after_user_id)
        .order_by(table.c.user_id)
        .limit(limit)
    )
    return [(user_id, token) for user_id, token in rows.all()]


async def existing_user_ids(db: AsyncSession, user_ids: Sequence[int]) -> set[int]:
    found: set[int] = set()
    for chunk in _chunks(sorted(set(user_ids))):
        found.update((await db.execute(select(User.id).where(User.id.in_(chunk)))).scalars().all())
    return found


async def clear_node_removals(db: AsyncSession, removals: Iterable[Removal]) -> None:
    table = PendingNodeRemoval.__table__
    for chunk in _chunks(sorted(set(removals))):
        await db.execute(delete(table).where(tuple_(table.c.user_id, table.c.token).in_(chunk)))
