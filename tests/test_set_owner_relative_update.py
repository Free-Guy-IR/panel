import os

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from app.db import base
from app.db.crud.admin import get_admin
from app.db.crud.user import get_user_by_id, set_owner
from app.db.models import Admin, AdminRole, User
from app.models.proxy import ProxyTable
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


async def _seed(factory) -> tuple[int, int, int]:
    async with factory() as session:
        operator_role = (await session.execute(select(AdminRole.id).where(AdminRole.name == "operator"))).scalar_one()
        old_owner = Admin(username="reseller_x", hashed_password="x", role_id=operator_role, used_traffic=2000)
        new_owner = Admin(username="reseller_y", hashed_password="y", role_id=operator_role, used_traffic=700)
        session.add_all([old_owner, new_owner])
        await session.flush()
        moved = User(
            username="moved_user",
            admin_id=old_owner.id,
            proxy_settings=ProxyTable().dict(no_obj=True),
            used_traffic=500,
        )
        session.add(moved)
        await session.commit()
        return moved.id, old_owner.id, new_owner.id


async def _admin_traffic(factory, admin_id: int) -> int:
    async with factory() as session:
        return (await session.execute(select(Admin.used_traffic).where(Admin.id == admin_id))).scalar_one()


async def test_an_increment_committed_during_the_owner_change_is_kept(session_factory):
    user_id, old_owner_id, new_owner_id = await _seed(session_factory)

    async with session_factory() as owner_change:
        new_owner = await get_admin(owner_change, "reseller_y")
        db_user = await get_user_by_id(owner_change, user_id)
        assert db_user.admin.id == old_owner_id
        assert db_user.admin.used_traffic == 2000
        assert new_owner.used_traffic == 700

        async with session_factory() as usage_tick:
            await usage_tick.execute(
                update(Admin).where(Admin.id == old_owner_id).values(used_traffic=Admin.used_traffic + 300)
            )
            await usage_tick.commit()

        moved = await set_owner(owner_change, db_user, new_owner)
        assert moved.admin_id == new_owner_id

    assert await _admin_traffic(session_factory, old_owner_id) == 1800
    assert await _admin_traffic(session_factory, new_owner_id) == 1200


async def test_setting_the_same_owner_leaves_the_counters_alone(session_factory):
    user_id, old_owner_id, new_owner_id = await _seed(session_factory)

    async with session_factory() as session:
        db_user = await get_user_by_id(session, user_id)
        same_owner = await get_admin(session, "reseller_x")
        await set_owner(session, db_user, same_owner)

    assert await _admin_traffic(session_factory, old_owner_id) == 2000
    assert await _admin_traffic(session_factory, new_owner_id) == 700
