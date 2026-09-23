import os

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from app.db import base
from app.db.crud.admin import reset_admin_usage
from app.db.models import Admin, AdminRole, AdminStatus, AdminUsageLogs
from config import database_settings


def _database_url() -> str:
    if os.getenv("TEST_FROM", "local").lower() == "local":
        return "sqlite+aiosqlite:///:memory:"
    return database_settings.url


@pytest.fixture
async def session_factory():
    url = _database_url()
    if url.startswith("sqlite"):
        engine = create_async_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    else:
        engine = create_async_engine(url, poolclass=NullPool)

    proxy_column = base.Base.metadata.tables["users"].c.proxy_settings
    proxy_default = proxy_column.server_default
    if url.startswith("mysql"):
        proxy_column.server_default = None

    async with engine.begin() as conn:
        await conn.run_sync(base.Base.metadata.drop_all)
        await conn.run_sync(base.Base.metadata.create_all)

    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    async with factory() as seed:
        seed.add_all(
            [
                AdminRole(name="owner", is_owner=True, permissions={}, limits={}, features={}, access={}),
                AdminRole(name="administrator", is_owner=False, permissions={}, limits={}, features={}, access={}),
                AdminRole(name="operator", is_owner=False, permissions={}, limits={}, features={}, access={}),
            ]
        )
        await seed.commit()

    try:
        yield factory
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(base.Base.metadata.drop_all)
            await conn.run_sync(base.Base.metadata.create_all)
        await engine.dispose()
        proxy_column.server_default = proxy_default


async def _seed_admin(factory, *, used_traffic: int, status: AdminStatus = AdminStatus.active) -> int:
    async with factory() as session:
        operator_role = (await session.execute(select(AdminRole.id).where(AdminRole.name == "operator"))).scalar_one()
        admin = Admin(
            username=f"reseller{used_traffic}",
            hashed_password="x",
            role_id=operator_role,
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


async def test_bytes_billed_between_the_read_and_the_reset_survive(session_factory):
    admin_id = await _seed_admin(session_factory, used_traffic=1000)

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


async def test_a_limited_admin_is_reactivated_by_the_reset(session_factory):
    admin_id = await _seed_admin(session_factory, used_traffic=4096, status=AdminStatus.limited)

    async with session_factory() as session:
        db_admin = (await session.execute(select(Admin).where(Admin.id == admin_id))).scalar_one()
        returned = await reset_admin_usage(session, db_admin)
        assert returned.status == AdminStatus.active
        assert returned.last_status_change is not None

    used_traffic, status, logged = await _stored(session_factory, admin_id)
    assert used_traffic == 0
    assert status == AdminStatus.active
    assert logged == [4096]


async def test_an_admin_with_no_usage_writes_no_log(session_factory):
    admin_id = await _seed_admin(session_factory, used_traffic=0)

    async with session_factory() as session:
        db_admin = (await session.execute(select(Admin).where(Admin.id == admin_id))).scalar_one()
        returned = await reset_admin_usage(session, db_admin)
        assert returned.used_traffic == 0

    used_traffic, status, logged = await _stored(session_factory, admin_id)
    assert used_traffic == 0
    assert status == AdminStatus.active
    assert logged == []
