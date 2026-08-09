"""Resetting or deleting a hardware id clears the user's device-review row.

The row is only rewritten while the user is being checked, so without this the
review keeps showing the devices that were just cleared - and for a user
outside the connection-limit scope, keeps showing them for good.
"""

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Admin, Base, User, UserConnectionState, UserHWID
from app.models.proxy import ProxyTable
from app.operation.hwid import _clear_connection_state


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


async def _user_with_state(db):
    admin = Admin(username="owner", hashed_password="x", role_id=1)
    db.add(admin)
    await db.flush()
    user = User(username="someone", proxy_settings=ProxyTable().dict(), admin_id=admin.id)
    db.add(user)
    await db.flush()
    db.add(UserConnectionState(user_id=user.id, devices=3, hwid_count=2, verdict="over_limit"))
    await db.commit()
    return user.id


async def _has_state(db, user_id):
    return (await db.scalar(select(UserConnectionState.user_id).where(UserConnectionState.user_id == user_id))) is not None


@pytest.mark.asyncio
async def test_clearing_devices_removes_the_review_row(db):
    user_id = await _user_with_state(db)
    assert await _has_state(db, user_id)

    await _clear_connection_state(db, user_id)

    assert not await _has_state(db, user_id)


@pytest.mark.asyncio
async def test_clearing_when_there_is_no_row_is_harmless(db):
    """A user the limiter never checked has no row; clearing must not error."""
    admin = Admin(username="o2", hashed_password="x", role_id=1)
    db.add(admin)
    await db.flush()
    user = User(username="never-checked", proxy_settings=ProxyTable().dict(), admin_id=admin.id)
    db.add(user)
    await db.commit()

    await _clear_connection_state(db, user.id)  # no raise

    assert not await _has_state(db, user.id)


@pytest.mark.asyncio
async def test_only_that_users_row_is_cleared(db):
    a = await _user_with_state(db)
    admin = (await db.execute(select(Admin))).scalars().first()
    other = User(username="other", proxy_settings=ProxyTable().dict(), admin_id=admin.id)
    db.add(other)
    await db.flush()
    db.add(UserConnectionState(user_id=other.id, devices=1, verdict="within_limit"))
    await db.commit()

    await _clear_connection_state(db, a)

    assert not await _has_state(db, a)
    assert await _has_state(db, other.id)
