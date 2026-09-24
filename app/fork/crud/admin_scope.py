from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User
from app.fork.crud.group_scope import user_in_allowed_groups


async def visible_admin_user_ids(db: AsyncSession, admin_id: int, allowed_group_ids: list[int]) -> list[int]:
    stmt = select(User.id).where(User.admin_id == admin_id, user_in_allowed_groups(User.id, allowed_group_ids))
    return list((await db.execute(stmt)).scalars().all())


async def count_visible_admin_users(db: AsyncSession, admin_id: int, allowed_group_ids: list[int]) -> int:
    stmt = select(func.count(User.id)).where(
        User.admin_id == admin_id, user_in_allowed_groups(User.id, allowed_group_ids)
    )
    return int((await db.execute(stmt)).scalar() or 0)
