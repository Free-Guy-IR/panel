"""The job loads the user itself, so nothing about it is in memory beforehand.

The earlier tests built the user in the same session, which left its
relationships loaded and hid what happened in production: reading an unloaded
relationship under the async session raises MissingGreenlet, the cycle died
there, and no restriction was ever applied. The push here reads exactly what
update_user() reads, so the loader has to cover all of it, both ways round.
"""

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Admin, Base, ConnectionRestriction, Group, User, UserStatus
from app.models.proxy import ProxyTable
from app.models.settings import ConnectionLimit
from app.models.user import UserNotificationResponse
from app.utils.connection_enforcement import restrict


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


class _Observation:
    def __init__(self, user_id, streak, verdict="over_limit"):
        self.user_id = user_id
        self.streak = streak
        self.verdict = verdict
        self.devices = 5
        self.limit_applied = 2
        self.details = {"real_groups": ["5.115.21.0/24"]}
        self.reasons = [{"code": "allowance", "count": 2}]


async def _stored_user(db) -> tuple[User, int]:
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
    return user, group.id


def _what_update_user_reads(users):
    """What _push_to_nodes -> update_user -> validate_user touches on each user."""
    for user in users:
        UserNotificationResponse.model_validate(user)
        assert user.admin is not None
        _ = user.next_plan, user.lifetime_used_traffic, user.group_ids


@pytest.mark.asyncio
async def test_a_user_the_job_loads_cold_is_restricted_and_can_be_pushed(db):
    from app.jobs.connection_limiter import _enforce

    user, group_id = await _stored_user(db)
    user_id = user.id
    db.expunge_all()  # the job starts from a cold session: nothing is in memory
    settings = ConnectionLimit(enforcement_enabled=True, persistence_cycles=3, punishment_steps=[-1])
    pushed = AsyncMock(side_effect=_what_update_user_reads)

    with patch("app.jobs.connection_limiter._push_to_nodes", new=pushed):
        await _enforce(db, [_Observation(user_id, streak=3)], settings)

    row = (await db.execute(select(ConnectionRestriction))).scalar()
    assert row is not None and row.active is True
    assert row.previous_group_ids == [group_id]
    assert (await db.get(User, user_id)).status == UserStatus.disabled
    pushed.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_warning_for_a_cold_loaded_user_is_written_down_too(db):
    from app.jobs.connection_limiter import _enforce

    user, group_id = await _stored_user(db)
    user_id = user.id
    db.expunge_all()
    settings = ConnectionLimit(enforcement_enabled=True, persistence_cycles=1, punishment_steps=[0, 10])

    with patch("app.jobs.connection_limiter._push_to_nodes", new=AsyncMock()):
        await _enforce(db, [_Observation(user_id, streak=1)], settings)

    row = (await db.execute(select(ConnectionRestriction))).scalar()
    assert row is not None and row.active is False and row.previous_group_ids == [group_id]
    assert (await db.get(User, user_id)).status == UserStatus.active


@pytest.mark.asyncio
async def test_a_cold_loaded_user_whose_time_is_up_is_released_and_can_be_pushed(db):
    from app.jobs.connection_limiter import _release_due

    user, _ = await _stored_user(db)
    user_id = user.id
    await restrict(db, user, _Observation(user_id, 3), ConnectionLimit(punishment_steps=[-1]))
    await db.commit()
    db.expunge_all()
    pushed = AsyncMock(side_effect=_what_update_user_reads)

    with patch("app.jobs.connection_limiter._push_to_nodes", new=pushed):
        await _release_due(db, ConnectionLimit(enforcement_enabled=False))

    assert (await db.get(User, user_id)).status == UserStatus.active
    pushed.assert_awaited_once()
