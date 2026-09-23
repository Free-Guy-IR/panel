from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import users_groups_association
from app.operation.permissions import get_allowed_group_ids


async def user_within_allowed_groups(db: AsyncSession, user_id: int, admin) -> bool:
    allowed_group_ids = get_allowed_group_ids(admin)
    if allowed_group_ids is None:
        return True
    if not allowed_group_ids:
        return False
    stmt = (
        select(users_groups_association.c.user_id)
        .where(
            users_groups_association.c.user_id == user_id,
            users_groups_association.c.groups_id.in_(allowed_group_ids),
        )
        .limit(1)
    )
    return (await db.execute(stmt)).first() is not None
