from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import User
from app.models.user import BulkUserFilter


async def get_users_for_mtproto_activation(db: AsyncSession, bulk_model: BulkUserFilter) -> list[User]:
    """Users matched by the shared bulk filter, with groups eagerly loaded.

    Groups are needed by app.utils.mtproto.prepare_mtproto_secret's access
    check (via tags_from_groups) in the caller - loading them here avoids an
    N+1 lazy-load per user. Unlike the other count_bulk_*/update_users_*
    helpers above, eligibility for MTProto activation isn't expressible as a
    single SQL WHERE clause (it depends on the group's mtproto-access tags,
    resolved in Python), so this only returns candidates for the caller to
    filter further - it does not itself decide who gets a secret.
    """
    from app.db.crud.bulk import _create_final_filter

    final_filter = _create_final_filter(bulk_model)
    result = await db.execute(select(User).where(final_filter).options(selectinload(User.groups)))
    return list(result.scalars().all())


async def get_users_for_l2tp_activation(db: AsyncSession, bulk_model: BulkUserFilter) -> list[User]:
    from app.db.crud.bulk import _create_final_filter

    final_filter = _create_final_filter(bulk_model)
    result = await db.execute(select(User).where(final_filter).options(selectinload(User.groups)))
    return list(result.scalars().all())
