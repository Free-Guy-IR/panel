from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import User
from app.models.user import BulkUserFilter


async def get_users_for_l2tp_activation(
    db: AsyncSession, bulk_model: BulkUserFilter, allowed_group_ids: list[int] | None = None
) -> list[User]:
    from app.db.crud.bulk import _create_final_filter

    final_filter = _create_final_filter(bulk_model, allowed_group_ids)
    result = await db.execute(select(User).where(final_filter).options(selectinload(User.groups)))
    return list(result.scalars().all())
