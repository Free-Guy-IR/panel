from datetime import UTC, datetime

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import Admin, AdminStatus


def admin_over_quota():
    return and_(Admin.data_limit.isnot(None), Admin.data_limit > 0, Admin.used_traffic >= Admin.data_limit)


async def get_active_to_limited_admin_ids(db: AsyncSession) -> list[int]:
    stmt = select(Admin.id).where(Admin.status == AdminStatus.active, admin_over_quota()).order_by(Admin.id)
    return list((await db.execute(stmt)).scalars().all())


async def lock_admin_row(db: AsyncSession, db_admin: Admin) -> Admin:
    await db.refresh(db_admin, attribute_names=["status", "used_traffic", "data_limit"], with_for_update=True)
    return db_admin


async def lock_admin_status(db: AsyncSession, admin_id: int) -> AdminStatus | None:
    stmt = select(Admin.status).where(Admin.id == admin_id).with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def limit_admin_if_over_quota(db: AsyncSession, admin_id: int) -> Admin | None:
    locked_id = (
        await db.execute(
            select(Admin.id)
            .where(Admin.id == admin_id, Admin.status == AdminStatus.active, admin_over_quota())
            .with_for_update()
        )
    ).scalar_one_or_none()
    if locked_id is None:
        await db.commit()
        return None

    result = await db.execute(
        update(Admin)
        .where(Admin.id == admin_id, Admin.status == AdminStatus.active, admin_over_quota())
        .values(status=AdminStatus.limited, last_status_change=datetime.now(UTC))
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await db.rollback()
        return None
    await db.commit()

    stmt = (
        select(Admin)
        .options(selectinload(Admin.role))
        .where(Admin.id == admin_id)
        .execution_options(populate_existing=True)
    )
    return (await db.execute(stmt)).scalar_one_or_none()
