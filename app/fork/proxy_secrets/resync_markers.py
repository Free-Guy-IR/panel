from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import delete, literal, select, tuple_
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.db import AsyncSession
from app.db.models import User, users_groups_association
from app.fork.models.pending_user_resync import PendingUserResync
from app.fork.proxy_secrets.locks import MYSQL_FAMILY

MARKER_CHUNK = 1000

Marker = tuple[int, str]


def _chunks(values: Sequence, size: int = MARKER_CHUNK) -> Iterable[Sequence]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _mark_statement(dialect_name: str, user_ids: Sequence[int], token: str, requested_at: datetime):
    table = PendingUserResync.__table__
    source = select(User.id, literal(token), literal(requested_at, table.c.requested_at.type)).where(
        User.id.in_(user_ids)
    )
    columns = ["user_id", "token", "requested_at"]
    refreshed = {"token": token, "requested_at": requested_at}
    if dialect_name in MYSQL_FAMILY:
        return mysql_insert(table).from_select(columns, source).on_duplicate_key_update(**refreshed)
    if dialect_name == "postgresql":
        return pg_insert(table).from_select(columns, source).on_conflict_do_update(
            index_elements=["user_id"], set_=refreshed
        )
    return sqlite_insert(table).from_select(columns, source).on_conflict_do_update(
        index_elements=["user_id"], set_=refreshed
    )


async def mark_users(db: AsyncSession, user_ids: Iterable[int]) -> None:
    ordered = sorted(set(user_ids))
    if not ordered:
        return
    token = uuid4().hex
    requested_at = datetime.now(UTC)
    dialect_name = db.get_bind().dialect.name
    for chunk in _chunks(ordered):
        await db.execute(_mark_statement(dialect_name, chunk, token, requested_at))


async def mark_group_members(db: AsyncSession, group_ids: Iterable[int]) -> None:
    wanted = sorted(set(group_ids))
    if not wanted:
        return
    column = users_groups_association.c.user_id
    members = await db.execute(select(column).where(users_groups_association.c.groups_id.in_(wanted)).distinct())
    await mark_users(db, members.scalars().all())


async def mark_bulk_group_scope(db: AsyncSession, bulk_model) -> None:
    from app.db.crud.bulk import _create_group_filter

    scope = await db.execute(select(User.id).where(_create_group_filter(bulk_model)))
    await mark_users(db, scope.scalars().all())


async def markers_for(db: AsyncSession, user_ids: Iterable[int]) -> list[Marker]:
    table = PendingUserResync.__table__
    found: list[Marker] = []
    for chunk in _chunks(sorted(set(user_ids))):
        rows = await db.execute(select(table.c.user_id, table.c.token).where(table.c.user_id.in_(chunk)))
        found.extend((user_id, token) for user_id, token in rows.all())
    return found


async def due_markers(
    db: AsyncSession, requested_before: datetime, limit: int, *, after_user_id: int = 0
) -> list[Marker]:
    table = PendingUserResync.__table__
    rows = await db.execute(
        select(table.c.user_id, table.c.token)
        .where(table.c.requested_at <= requested_before, table.c.user_id > after_user_id)
        .order_by(table.c.user_id)
        .limit(limit)
    )
    return [(user_id, token) for user_id, token in rows.all()]


async def clear_markers(db: AsyncSession, markers: Iterable[Marker]) -> None:
    table = PendingUserResync.__table__
    for chunk in _chunks(sorted(set(markers))):
        await db.execute(delete(table).where(tuple_(table.c.user_id, table.c.token).in_(chunk)))
