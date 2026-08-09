"""The switch is the whole contract: off means nothing happens to anyone.

These go through the job's own helpers rather than the engine underneath, so
what is checked is the thing that actually runs on a schedule.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Admin, Base, ConnectionRestriction, Group, User, UserStatus
from app.models.proxy import ProxyTable
from app.models.settings import ConnectionLimit
from app.utils.connection_enforcement import restrict


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


async def _user(db) -> User:
    admin = Admin(username="owner", hashed_password="x", role_id=1)
    db.add(admin)
    await db.flush()
    group = Group(name="blue", inbounds=[])
    db.add(group)
    await db.flush()
    user = User(username="someone", proxy_settings=ProxyTable().dict(), admin_id=admin.id)
    user.groups = [group]
    user.status = UserStatus.active
    db.add(user)
    await db.commit()
    return user


class _Observation:
    """Only what enforcement reads off an observation."""

    def __init__(self, user_id, streak, verdict="over_limit"):
        self.user_id = user_id
        self.streak = streak
        self.verdict = verdict
        self.devices = 5
        self.limit_applied = 2
        self.details = {"real_groups": ["5.115.21.0/24"]}


@pytest.mark.asyncio
async def test_a_verdict_that_has_not_held_long_enough_is_left_alone(db):
    """One cycle proves nothing - an address changes, wifi becomes mobile."""
    from app.jobs.connection_limiter import _enforce

    user = await _user(db)
    settings = ConnectionLimit(enforcement_enabled=True, persistence_cycles=3, punishment_steps=[-1])

    with patch("app.jobs.connection_limiter._push_to_nodes", new=AsyncMock()):
        await _enforce(db, [_Observation(user.id, streak=2)], settings)

    assert user.status == UserStatus.active
    assert (await db.scalar(select(ConnectionRestriction.id))) is None


@pytest.mark.asyncio
async def test_once_it_has_held_the_user_is_acted_on_and_the_nodes_are_told(db):
    from app.jobs.connection_limiter import _enforce

    user = await _user(db)
    settings = ConnectionLimit(enforcement_enabled=True, persistence_cycles=3, punishment_steps=[-1])
    pushed = AsyncMock()

    with patch("app.jobs.connection_limiter._push_to_nodes", new=pushed):
        await _enforce(db, [_Observation(user.id, streak=3)], settings)

    assert user.status == UserStatus.disabled
    pushed.assert_awaited_once()
    assert [u.id for u in pushed.await_args.args[0]] == [user.id]


@pytest.mark.asyncio
async def test_a_user_within_the_limit_is_never_acted_on(db):
    from app.jobs.connection_limiter import _enforce

    user = await _user(db)
    settings = ConnectionLimit(enforcement_enabled=True, persistence_cycles=1, punishment_steps=[-1])

    with patch("app.jobs.connection_limiter._push_to_nodes", new=AsyncMock()):
        await _enforce(db, [_Observation(user.id, streak=99, verdict="within_limit")], settings)

    assert user.status == UserStatus.active


@pytest.mark.asyncio
async def test_turning_the_switch_off_releases_whoever_is_still_held(db):
    """The one outcome nobody would expect is being left disabled by a feature
    that is no longer running."""
    from app.jobs.connection_limiter import _release_due

    user = await _user(db)
    await restrict(db, user, _Observation(user.id, 3), ConnectionLimit(punishment_steps=[-1]))
    await db.commit()
    assert user.status == UserStatus.disabled

    with patch("app.jobs.connection_limiter._push_to_nodes", new=AsyncMock()):
        await _release_due(db, ConnectionLimit(enforcement_enabled=False))

    assert user.status == UserStatus.active


@pytest.mark.asyncio
async def test_with_the_switch_on_a_restriction_still_running_is_left_in_place(db):
    from app.jobs.connection_limiter import _release_due

    user = await _user(db)
    settings = ConnectionLimit(enforcement_enabled=True, punishment_steps=[10])
    await restrict(db, user, _Observation(user.id, 3), settings)
    await db.commit()

    with patch("app.jobs.connection_limiter._push_to_nodes", new=AsyncMock()):
        await _release_due(db, settings)

    assert user.status == UserStatus.disabled


@pytest.mark.asyncio
async def test_when_its_time_is_up_it_lifts_by_itself(db):
    from app.jobs.connection_limiter import _release_due

    user = await _user(db)
    settings = ConnectionLimit(enforcement_enabled=True, punishment_steps=[10])
    await restrict(db, user, _Observation(user.id, 3), settings)
    row = (await db.execute(select(ConnectionRestriction))).scalar()
    row.restore_at = datetime.now(UTC) - timedelta(seconds=1)
    await db.commit()

    with patch("app.jobs.connection_limiter._push_to_nodes", new=AsyncMock()):
        await _release_due(db, settings)

    assert user.status == UserStatus.active
    assert row.active is False
