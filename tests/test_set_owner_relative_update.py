import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from app.db import base
from app.db.crud.admin import get_admin
from app.db.crud.user import get_user_by_id, set_owner
from app.db.models import Admin, AdminRole, User
from app.models.proxy import ProxyTable
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
async def owners(session_factory):
    suffix = uuid4().hex[:12]
    async with session_factory() as session:
        role = AdminRole(name=f"owner_move_{suffix}", is_owner=False, permissions={}, limits={}, features={}, access={})
        session.add(role)
        await session.flush()
        old_owner = Admin(username=f"old_{suffix}", hashed_password="x", role_id=role.id, used_traffic=2000)
        new_owner = Admin(username=f"new_{suffix}", hashed_password="y", role_id=role.id, used_traffic=700)
        session.add_all([old_owner, new_owner])
        await session.flush()
        moved = User(
            username=f"moved_{suffix}",
            admin_id=old_owner.id,
            proxy_settings=ProxyTable().dict(no_obj=True),
            used_traffic=500,
        )
        session.add(moved)
        await session.commit()
        seeded = SimpleNamespace(
            role_id=role.id,
            user_id=moved.id,
            old_owner_id=old_owner.id,
            old_owner_name=old_owner.username,
            new_owner_id=new_owner.id,
            new_owner_name=new_owner.username,
        )
    try:
        yield seeded
    finally:
        async with session_factory() as session:
            admin_ids = select(Admin.id).where(Admin.role_id == seeded.role_id).scalar_subquery()
            await session.execute(delete(User).where(User.admin_id.in_(admin_ids)))
            await session.execute(delete(Admin).where(Admin.role_id == seeded.role_id))
            await session.execute(delete(AdminRole).where(AdminRole.id == seeded.role_id))
            await session.commit()


async def _admin_traffic(factory, admin_id: int) -> int:
    async with factory() as session:
        return (await session.execute(select(Admin.used_traffic).where(Admin.id == admin_id))).scalar_one()


async def test_an_increment_committed_during_the_owner_change_is_kept(session_factory, owners):
    async with session_factory() as owner_change:
        new_owner = await get_admin(owner_change, owners.new_owner_name)
        db_user = await get_user_by_id(owner_change, owners.user_id)
        assert db_user.admin.id == owners.old_owner_id
        assert db_user.admin.used_traffic == 2000
        assert new_owner.used_traffic == 700

        async with session_factory() as usage_tick:
            await usage_tick.execute(
                update(Admin).where(Admin.id == owners.old_owner_id).values(used_traffic=Admin.used_traffic + 300)
            )
            await usage_tick.commit()

        moved = await set_owner(owner_change, db_user, new_owner)
        assert moved.admin_id == owners.new_owner_id

    assert await _admin_traffic(session_factory, owners.old_owner_id) == 1800
    assert await _admin_traffic(session_factory, owners.new_owner_id) == 1200


async def test_setting_the_same_owner_leaves_the_counters_alone(session_factory, owners):
    async with session_factory() as session:
        db_user = await get_user_by_id(session, owners.user_id)
        same_owner = await get_admin(session, owners.old_owner_name)
        await set_owner(session, db_user, same_owner)

    assert await _admin_traffic(session_factory, owners.old_owner_id) == 2000
    assert await _admin_traffic(session_factory, owners.new_owner_id) == 700
