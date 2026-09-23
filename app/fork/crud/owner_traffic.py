from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Admin, User

OWNER_LOCK_BATCH_SIZE = 500


async def lock_owner_rows(db: AsyncSession, users: list[User]) -> dict[int, tuple[int | None, int]]:
    user_ids = sorted({db_user.id for db_user in users if db_user.id is not None})
    locked: dict[int, tuple[int | None, int]] = {}
    for index in range(0, len(user_ids), OWNER_LOCK_BATCH_SIZE):
        chunk = user_ids[index : index + OWNER_LOCK_BATCH_SIZE]
        stmt = (
            select(User.id, User.admin_id, User.used_traffic)
            .where(User.id.in_(chunk))
            .order_by(User.id)
            .with_for_update()
        )
        for user_id, admin_id, used_traffic in (await db.execute(stmt)).all():
            locked[user_id] = (admin_id, int(used_traffic or 0))
    return locked


async def shift_admin_traffic(db: AsyncSession, changes: dict[int, int]) -> None:
    for admin_id in sorted(changes):
        if changes[admin_id]:
            await db.execute(
                update(Admin)
                .where(Admin.id == admin_id)
                .values(used_traffic=Admin.used_traffic + changes[admin_id])
                .execution_options(synchronize_session=False)
            )
