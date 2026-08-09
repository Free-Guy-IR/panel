"""Escalation, and putting the user back afterwards.

The steps are the same shape as PG-Limiter's: a warning, then disables of
growing length, then one that only a person can lift. Which step applies comes
from the violations already inside the window, so a user who behaves for long
enough starts again from the beginning without anyone clearing anything.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Admin, Base, ConnectionRestriction, Group, User, UserStatus
from app.models.proxy import ProxyTable
from app.models.settings import ConnectionLimit
from app.utils.connection_enforcement import (
    due_to_restore,
    forget_old,
    release,
    restrict,
    step_for,
    violations_in_window,
)

STEPS = [0, 10, 30, 60, -1]
OBSERVED = SimpleNamespace(devices=5, limit_applied=2, details={"real_groups": ["5.115.21.0/24"]})


# --------------------------------------------------------------- the ladder --

@pytest.mark.parametrize(
    "already, expected",
    [(0, (0, 0)), (1, (1, 10)), (2, (2, 30)), (3, (3, 60)), (4, (4, -1))],
)
def test_each_violation_moves_one_rung_up(already, expected):
    assert step_for(already, STEPS) == expected


def test_past_the_end_the_last_rung_repeats():
    """The list runs out before repeat offenders do."""
    assert step_for(99, STEPS) == (4, -1)


def test_no_steps_configured_is_a_warning_and_nothing_else():
    assert step_for(3, []) == (0, 0)


# ------------------------------------------------------------- against a db --

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


@pytest.mark.asyncio
async def test_the_first_violation_only_warns(db):
    user = await _user(db)
    assert await restrict(db, user, OBSERVED, ConnectionLimit(punishment_steps=STEPS)) is None
    await db.commit()

    assert user.status == UserStatus.active
    row = (await db.execute(select(ConnectionRestriction))).scalar()
    assert (row.step_applied, row.disable_minutes, row.active) == (0, 0, False)


@pytest.mark.asyncio
async def test_the_second_disables_for_the_second_step(db):
    user = await _user(db)
    settings = ConnectionLimit(punishment_steps=STEPS)
    await restrict(db, user, OBSERVED, settings)
    await db.commit()

    assert await restrict(db, user, OBSERVED, settings) is not None
    await db.commit()

    assert user.status == UserStatus.disabled
    row = (await db.execute(select(ConnectionRestriction).order_by(ConnectionRestriction.id.desc()))).scalar()
    assert (row.step_applied, row.disable_minutes, row.active) == (1, 10, True)
    assert row.restore_at is not None
    assert row.previous_status == "active"


@pytest.mark.asyncio
async def test_a_user_already_restricted_is_not_restricted_again(db):
    user = await _user(db)
    settings = ConnectionLimit(punishment_steps=STEPS)
    await restrict(db, user, OBSERVED, settings)
    await db.commit()
    await restrict(db, user, OBSERVED, settings)
    await db.commit()

    assert await restrict(db, user, OBSERVED, settings) is None
    assert (await db.scalar(select(ConnectionRestriction.id).order_by(ConnectionRestriction.id.desc()))) == 2


@pytest.mark.asyncio
async def test_the_last_step_leaves_nothing_to_restore_it(db):
    user = await _user(db)
    settings = ConnectionLimit(punishment_steps=STEPS)
    for _ in range(4):
        await restrict(db, user, OBSERVED, settings)
        await db.commit()
        for row in (await db.execute(select(ConnectionRestriction).where(ConnectionRestriction.active.is_(True)))).scalars():
            release(row, user)
        await db.commit()

    await restrict(db, user, OBSERVED, settings)
    await db.commit()
    row = (await db.execute(select(ConnectionRestriction).order_by(ConnectionRestriction.id.desc()))).scalar()
    assert (row.disable_minutes, row.restore_at) == (-1, None)


@pytest.mark.asyncio
async def test_violations_outside_the_window_do_not_count(db):
    """Behave for long enough and the ladder starts from the bottom again."""
    user = await _user(db)
    old = ConnectionRestriction(user_id=user.id, active=False, step_applied=3)
    old.created_at = datetime.now(UTC) - timedelta(hours=100)
    db.add(old)
    await db.commit()

    assert await violations_in_window(db, user.id, 72) == 0
    assert await restrict(db, user, OBSERVED, ConnectionLimit(punishment_steps=STEPS)) is None


# --------------------------------------------------------------- restoring --

@pytest.mark.asyncio
async def test_time_served_puts_back_exactly_what_was_there(db):
    user = await _user(db)
    settings = ConnectionLimit(punishment_steps=STEPS)
    await restrict(db, user, OBSERVED, settings)
    await restrict(db, user, OBSERVED, settings)
    await db.commit()

    row = (await db.execute(select(ConnectionRestriction).where(ConnectionRestriction.active.is_(True)))).scalar()
    row.restore_at = datetime.now(UTC) - timedelta(seconds=1)
    await db.commit()

    pairs = await due_to_restore(db)
    assert len(pairs) == 1
    release(*pairs[0])
    await db.commit()

    assert user.status == UserStatus.active
    assert row.active is False and row.restored_at is not None


@pytest.mark.asyncio
async def test_a_restriction_still_running_is_left_alone(db):
    user = await _user(db)
    settings = ConnectionLimit(punishment_steps=STEPS)
    await restrict(db, user, OBSERVED, settings)
    await restrict(db, user, OBSERVED, settings)
    await db.commit()

    assert await due_to_restore(db) == []


@pytest.mark.asyncio
async def test_turning_enforcement_off_lets_everyone_go(db):
    """Including the ones with no restore time, which is the whole point."""
    user = await _user(db)
    settings = ConnectionLimit(punishment_steps=[-1])
    await restrict(db, user, OBSERVED, settings)
    await db.commit()
    assert user.status == UserStatus.disabled

    pairs = await due_to_restore(db, everyone=True)
    assert len(pairs) == 1
    release(*pairs[0])
    await db.commit()
    assert user.status == UserStatus.active


@pytest.mark.asyncio
async def test_old_history_is_forgotten_but_a_live_one_is_not(db):
    user = await _user(db)
    stale = ConnectionRestriction(user_id=user.id, active=False)
    stale.created_at = datetime.now(UTC) - timedelta(days=40)
    running = ConnectionRestriction(user_id=user.id, active=True)
    running.created_at = datetime.now(UTC) - timedelta(days=40)
    db.add_all([stale, running])
    await db.commit()

    assert await forget_old(db, 30) == 1
    await db.commit()
    assert (await db.scalar(select(ConnectionRestriction.id))) == running.id
