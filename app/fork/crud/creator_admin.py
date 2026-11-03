from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Admin

CREATOR_ADMIN_GONE_DETAIL = "Admin not found"


class CreatorAdminGone(LookupError):
    pass


async def lock_creator_admin(db: AsyncSession, admin_id: int | None) -> None:
    if admin_id is None:
        return
    if db.bind.dialect.name == "sqlite":
        result = await db.execute(
            update(Admin).where(Admin.id == admin_id).values(id=Admin.id).execution_options(synchronize_session=False)
        )
        if not result.rowcount:
            raise CreatorAdminGone(admin_id)
        return
    stmt = select(Admin.id).where(Admin.id == admin_id).with_for_update(read=True, key_share=True)
    if (await db.execute(stmt)).scalar_one_or_none() is None:
        raise CreatorAdminGone(admin_id)
