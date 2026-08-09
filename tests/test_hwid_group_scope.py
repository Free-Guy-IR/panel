"""The HWID policy covers the chosen groups and nobody else.

Empty is everyone, which is what every existing panel already has. With groups
chosen, a user outside them is simply not covered - no registration, no header
requirement, no limit - whoever their admin happens to be.
"""

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Admin, Base, Group, User
from app.models.proxy import ProxyTable
from app.models.settings import HWIDSettings
from app.utils.hwid import hwid_covers_user


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


async def _setup(db):
    admin = Admin(username="owner", hashed_password="x", role_id=1)
    db.add(admin)
    await db.flush()
    covered_group, other_group = Group(name="namhdodasli", inbounds=[]), Group(name="other", inbounds=[])
    db.add_all([covered_group, other_group])
    await db.flush()

    users = {}
    for name, group in (("inside", covered_group), ("outside", other_group)):
        user = User(username=name, proxy_settings=ProxyTable().dict(), admin_id=admin.id)
        user.groups = [group]
        db.add(user)
        await db.flush()
        users[name] = user.id
    await db.commit()
    return covered_group.id, users


@pytest.mark.asyncio
async def test_no_groups_chosen_covers_everyone(db):
    _, users = await _setup(db)
    settings = HWIDSettings(enabled=True)
    assert await hwid_covers_user(db, users["inside"], settings)
    assert await hwid_covers_user(db, users["outside"], settings)


@pytest.mark.asyncio
async def test_a_member_of_a_chosen_group_is_covered(db):
    group_id, users = await _setup(db)
    settings = HWIDSettings(enabled=True, apply_to_group_ids=[group_id])
    assert await hwid_covers_user(db, users["inside"], settings)


@pytest.mark.asyncio
async def test_everyone_else_is_not(db):
    """The point of the feature: outside the groups, no policy at all."""
    group_id, users = await _setup(db)
    settings = HWIDSettings(enabled=True, apply_to_group_ids=[group_id])
    assert not await hwid_covers_user(db, users["outside"], settings)


@pytest.mark.asyncio
async def test_membership_in_any_chosen_group_is_enough(db):
    group_id, users = await _setup(db)
    settings = HWIDSettings(enabled=True, apply_to_group_ids=[group_id, 9999])
    assert await hwid_covers_user(db, users["inside"], settings)


@pytest.mark.asyncio
async def test_older_settings_without_the_field_cover_everyone(db):
    """Rows saved before the field existed must keep meaning what they meant."""
    _, users = await _setup(db)
    stored = HWIDSettings.model_validate({"enabled": True, "forced": False})
    assert await hwid_covers_user(db, users["outside"], stored)
