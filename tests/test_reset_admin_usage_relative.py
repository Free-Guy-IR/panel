import os
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from app.db import base
from app.db.crud.admin import reset_admin_usage
from app.db.models import Admin, AdminRole, AdminStatus, AdminUsageLogs
from config import database_settings


@pytest.fixture
async def session_factory():
    if os.getenv("TEST_FROM", "local").lower() == "local":
        engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        async with engine.begin() as conn:
            await conn.run_sync(base.Base.metadata.create_all)
    else:
        engine = create_async_engine(database_settings.url, poolclass=NullPool)
    try:
        yield async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    finally:
        await engine.dispose()


@pytest.fixture
async def role_id(session_factory):
    async with session_factory() as session:
        role = AdminRole(
            name=f"reset_{uuid4().hex[:12]}", is_owner=False, permissions={}, limits={}, features={}, access={}
        )
        session.add(role)
        await session.commit()
        created_role_id = role.id
    try:
        yield created_role_id
    finally:
        async with session_factory() as session:
            admin_ids = select(Admin.id).where(Admin.role_id == created_role_id).scalar_subquery()
            await session.execute(delete(AdminUsageLogs).where(AdminUsageLogs.admin_id.in_(admin_ids)))
            await session.execute(delete(Admin).where(Admin.role_id == created_role_id))
            await session.execute(delete(AdminRole).where(AdminRole.id == created_role_id))
            await session.commit()


async def _seed_admin(factory, role_id: int, *, used_traffic: int, status: AdminStatus = AdminStatus.active) -> int:
    async with factory() as session:
        admin = Admin(
            username=f"reset_{uuid4().hex[:12]}",
            hashed_password="x",
            role_id=role_id,
            used_traffic=used_traffic,
            status=status,
        )
        session.add(admin)
        await session.commit()
        return admin.id


async def _stored(factory, admin_id: int) -> tuple[int, AdminStatus, list[int]]:
    async with factory() as session:
        used_traffic, status = (
            await session.execute(select(Admin.used_traffic, Admin.status).where(Admin.id == admin_id))
        ).one()
        logged = (
            (
                await session.execute(
                    select(AdminUsageLogs.used_traffic_at_reset)
                    .where(AdminUsageLogs.admin_id == admin_id)
                    .order_by(AdminUsageLogs.id)
                )
            )
            .scalars()
            .all()
        )
        return used_traffic, status, list(logged)


async def test_bytes_billed_between_the_read_and_the_reset_survive(session_factory, role_id):
    admin_id = await _seed_admin(session_factory, role_id, used_traffic=1000)

    async with session_factory() as reset_session:
        db_admin = (await reset_session.execute(select(Admin).where(Admin.id == admin_id))).scalar_one()
        assert db_admin.used_traffic == 1000

        async with session_factory() as tick_session:
            await tick_session.execute(
                update(Admin).where(Admin.id == admin_id).values(used_traffic=Admin.used_traffic + 300)
            )
            await tick_session.commit()

        returned = await reset_admin_usage(reset_session, db_admin)
        assert returned.used_traffic == 300

    used_traffic, _, logged = await _stored(session_factory, admin_id)
    assert used_traffic == 300
    assert logged == [1000]


async def test_a_limited_admin_is_reactivated_by_the_reset(session_factory, role_id):
    admin_id = await _seed_admin(session_factory, role_id, used_traffic=4096, status=AdminStatus.limited)

    async with session_factory() as session:
        db_admin = (await session.execute(select(Admin).where(Admin.id == admin_id))).scalar_one()
        returned = await reset_admin_usage(session, db_admin)
        assert returned.status == AdminStatus.active
        assert returned.last_status_change is not None

    used_traffic, status, logged = await _stored(session_factory, admin_id)
    assert used_traffic == 0
    assert status == AdminStatus.active
    assert logged == [4096]


async def test_an_admin_with_no_usage_writes_no_log(session_factory, role_id):
    admin_id = await _seed_admin(session_factory, role_id, used_traffic=0)

    async with session_factory() as session:
        db_admin = (await session.execute(select(Admin).where(Admin.id == admin_id))).scalar_one()
        returned = await reset_admin_usage(session, db_admin)
        assert returned.used_traffic == 0

    used_traffic, status, logged = await _stored(session_factory, admin_id)
    assert used_traffic == 0
    assert status == AdminStatus.active
    assert logged == []
