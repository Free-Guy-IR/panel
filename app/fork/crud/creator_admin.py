from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Admin, User
from app.fork.proxy_secrets.locks import read_committed_rows

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


async def committed_users_count_by_admin(db: AsyncSession, admin_id: int | None) -> int:
    stmt = select(func.count(User.id))
    if admin_id is not None:
        stmt = stmt.where(User.admin_id == admin_id)
    return (await read_committed_rows(db, stmt))[0][0] or 0
