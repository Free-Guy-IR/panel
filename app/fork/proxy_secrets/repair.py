from __future__ import annotations

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import selectinload

from app.db import AsyncSession
from app.db.models import User
from app.fork.proxy_secrets.fields import ProxySecretField, SecretFieldSpec
from app.fork.proxy_secrets.locks import read_committed_rows
from app.fork.proxy_secrets.uniqueness import secret_column


async def duplicate_secret_values(db: AsyncSession, spec: SecretFieldSpec) -> set[str]:
    column = secret_column(spec)
    stmt = (
        select(column, func.count(User.id))
        .where(and_(column.isnot(None), column != ""))
        .group_by(column)
        .having(func.count(User.id) > 1)
    )
    return {row[0] for row in await read_committed_rows(db, stmt) if row[0]}


async def get_users_with_duplicate_secrets(
    db: AsyncSession,
    shared: dict[ProxySecretField, tuple[SecretFieldSpec, set[str]]],
    bulk_model,
    allowed_group_ids: list[int] | None = None,
) -> list[User]:
    if not shared:
        return []

    from app.db.crud.bulk import _create_final_filter

    candidates = [secret_column(spec).in_(sorted(values)) for spec, values in shared.values() if values]
    if not candidates:
        return []

    matching = select(User.id).where(and_(_create_final_filter(bulk_model, allowed_group_ids), or_(*candidates)))
    user_ids = sorted({row[0] for row in await read_committed_rows(db, matching)})
    if not user_ids:
        return []
    stmt = select(User).where(User.id.in_(user_ids)).order_by(User.id).options(selectinload(User.groups))
    return list((await db.execute(stmt)).scalars().all())
