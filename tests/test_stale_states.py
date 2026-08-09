"""A reading old enough to be from superseded logic must not look current."""

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Admin, Base, Group, User, UserConnectionState
from app.models.proxy import ProxyTable
from app.models.settings import ConnectionLimit
from app.utils.connection_limiter import prune_out_of_scope


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


async def _user_with_state(db, name: str, age_hours: float) -> int:
    admin = (await db.execute(select(Admin))).scalar()
    if admin is None:
        admin = Admin(username="owner", hashed_password="x", role_id=1)
        db.add(admin)
        await db.flush()
    group = (await db.execute(select(Group))).scalar()
    if group is None:
        group = Group(name="blue", inbounds=[])
        db.add(group)
        await db.flush()

    user = User(username=name, proxy_settings=ProxyTable().dict(), admin_id=admin.id)
    user.groups = [group]
    user.status = "active"
    db.add(user)
    await db.flush()

    state = UserConnectionState(user_id=user.id, devices=3, verdict="over_limit")
    state.checked_at = datetime.now(UTC) - timedelta(hours=age_hours)
    db.add(state)
    await db.commit()
    return user.id


async def _remaining(db) -> set[int]:
    return set((await db.execute(select(UserConnectionState.user_id))).scalars().all())


@pytest.mark.asyncio
async def test_a_reading_from_hours_ago_is_dropped(db):
    """Those rows were reached by whatever the logic was at the time."""
    fresh = await _user_with_state(db, "fresh", age_hours=0.1)
    old = await _user_with_state(db, "old", age_hours=9)

    await prune_out_of_scope(db, ConnectionLimit(state_max_age_hours=6))
    await db.commit()

    assert await _remaining(db) == {fresh}
    assert old not in await _remaining(db)


@pytest.mark.asyncio
async def test_a_reading_inside_the_window_is_kept(db):
    recent = await _user_with_state(db, "recent", age_hours=5)

    await prune_out_of_scope(db, ConnectionLimit(state_max_age_hours=6))
    await db.commit()

    assert await _remaining(db) == {recent}


@pytest.mark.asyncio
async def test_the_window_is_the_setting(db):
    await _user_with_state(db, "someone", age_hours=9)

    await prune_out_of_scope(db, ConnectionLimit(state_max_age_hours=24))
    await db.commit()

    assert len(await _remaining(db)) == 1
