from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Admin, Base, ConnectionRestriction, Group, User, UserStatus
from app.fork.jobs import connection_limiter
from app.fork.operation.admin_scope import close_visible_restrictions
from app.models.admin import AdminDetails, AdminRoleData
from app.models.proxy import ProxyTable
from app.models.settings import ConnectionLimit
from app.utils.connection_enforcement import (
    close_active_restriction,
    restrict,
)

OWNER = AdminDetails(id=None, username="owner", role=AdminRoleData(id=1, name="owner", is_owner=True))
ENFORCING = ConnectionLimit(enforcement_enabled=True, monitor_only=False, persistence_cycles=1, punishment_steps=[30])


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


class _Observation:
    def __init__(self, user_id):
        self.user_id = user_id
        self.streak = 5
        self.verdict = "over_limit"
        self.devices = 5
        self.limit_applied = 2
        self.details = {"real_groups": ["1.2.3.4/32"]}
        self.reasons = [{"code": "allowance", "count": 2}]


async def _seed(session_factory, owners: int = 1, users_per_owner: int = 1) -> list[tuple[int, int]]:
    seeded = []
    async with session_factory() as db:
        group = Group(name="blue", inbounds=[])
        db.add(group)
        await db.flush()
        for owner_index in range(owners):
            admin = Admin(username=f"owner-{owner_index}", hashed_password="x", role_id=1)
            db.add(admin)
            await db.flush()
            for user_index in range(users_per_owner):
                user = User(
                    username=f"user-{owner_index}-{user_index}",
                    proxy_settings=ProxyTable().dict(),
                    admin_id=admin.id,
                )
                user.groups = [group]
                user.status = UserStatus.active
                user.last_status_change = datetime.now(UTC) - timedelta(days=3)
                db.add(user)
                await db.flush()
                seeded.append((admin.id, user.id))
        await db.commit()
    return seeded


async def _restrict_now(session_factory, user_id: int) -> None:
    async with session_factory() as db:
        user = (await db.execute(select(User).where(User.id == user_id))).unique().scalar_one()
        await db.refresh(user, ["groups"])
        restriction = await restrict(db, user, _Observation(user_id), ENFORCING)
        assert restriction is not None
        await db.commit()


async def _expire_restrictions(session_factory) -> None:
    async with session_factory() as db:
        for restriction in (await db.execute(select(ConnectionRestriction))).scalars().all():
            restriction.restore_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()


async def _admin_sets_status(session_factory, user_id: int, status: UserStatus, *, close_restriction: bool) -> None:
    async with session_factory() as db:
        user = await db.get(User, user_id)
        if user.status != status:
            user.status = status
            user.last_status_change = datetime.now(UTC)
        if close_restriction:
            await close_active_restriction(db, user_id, "an admin set the user's status")
        await db.commit()


async def _release(session_factory, settings) -> AsyncMock:
    pushed = AsyncMock()
    with patch.object(connection_limiter, "_push_to_nodes", new=pushed):
        async with session_factory() as db:
            await connection_limiter._release_due(db, settings)
    return pushed


async def _status_and_restriction(session_factory, user_id: int) -> tuple[UserStatus, bool]:
    async with session_factory() as db:
        status = (await db.execute(select(User.status).where(User.id == user_id))).scalar_one()
        active = (
            await db.execute(select(ConnectionRestriction.active).where(ConnectionRestriction.user_id == user_id))
        ).scalar_one()
    return status, active


def _pushed_ids(pushed: AsyncMock) -> list[int]:
    return [user.id for call in pushed.await_args_list for user in call.args[0]]


@pytest.mark.asyncio
async def test_an_expired_restriction_restores_the_status_the_limiter_replaced(session_factory):
    [(_, user_id)] = await _seed(session_factory)
    await _restrict_now(session_factory, user_id)
    assert (await _status_and_restriction(session_factory, user_id)) == (UserStatus.disabled, True)
    await _expire_restrictions(session_factory)

    pushed = await _release(session_factory, ENFORCING)

    assert (await _status_and_restriction(session_factory, user_id)) == (UserStatus.active, False)
    assert _pushed_ids(pushed) == [user_id]


@pytest.mark.asyncio
async def test_an_admin_disable_during_a_restriction_outlives_its_expiry(session_factory):
    [(_, user_id)] = await _seed(session_factory)
    await _restrict_now(session_factory, user_id)
    await _admin_sets_status(session_factory, user_id, UserStatus.disabled, close_restriction=True)
    await _expire_restrictions(session_factory)

    pushed = await _release(session_factory, ENFORCING)

    assert (await _status_and_restriction(session_factory, user_id)) == (UserStatus.disabled, False)
    assert _pushed_ids(pushed) == []


@pytest.mark.asyncio
async def test_a_status_decided_after_the_restriction_is_never_overwritten_by_its_release(session_factory):
    [(_, user_id)] = await _seed(session_factory)
    await _restrict_now(session_factory, user_id)
    await _admin_sets_status(session_factory, user_id, UserStatus.active, close_restriction=False)
    await _admin_sets_status(session_factory, user_id, UserStatus.disabled, close_restriction=False)
    await _expire_restrictions(session_factory)

    pushed = await _release(session_factory, ENFORCING)

    assert (await _status_and_restriction(session_factory, user_id)) == (UserStatus.disabled, False)
    assert _pushed_ids(pushed) == []


@pytest.mark.asyncio
async def test_an_admin_enable_during_a_restriction_is_kept_and_the_restriction_is_closed(session_factory):
    [(_, user_id)] = await _seed(session_factory)
    await _restrict_now(session_factory, user_id)
    await _admin_sets_status(session_factory, user_id, UserStatus.on_hold, close_restriction=False)

    pushed = await _release(session_factory, None)

    assert (await _status_and_restriction(session_factory, user_id)) == (UserStatus.on_hold, False)
    assert _pushed_ids(pushed) == []


@pytest.mark.asyncio
async def test_turning_enforcement_off_releases_only_what_the_limiter_still_owns(session_factory):
    [(_, owned_id), (_, overridden_id)] = await _seed(session_factory, users_per_owner=2)
    await _restrict_now(session_factory, owned_id)
    await _restrict_now(session_factory, overridden_id)
    await _admin_sets_status(session_factory, overridden_id, UserStatus.disabled, close_restriction=True)

    pushed = await _release(session_factory, None)

    assert (await _status_and_restriction(session_factory, owned_id)) == (UserStatus.active, False)
    assert (await _status_and_restriction(session_factory, overridden_id)) == (UserStatus.disabled, False)
    assert _pushed_ids(pushed) == [owned_id]


@pytest.mark.asyncio
async def test_closing_for_an_admin_closes_only_the_restrictions_of_that_admins_users(session_factory):
    [(first_admin, first_user), (_, second_user)] = await _seed(session_factory, owners=2)
    await _restrict_now(session_factory, first_user)
    await _restrict_now(session_factory, second_user)

    async with session_factory() as db:
        closed = await close_visible_restrictions(db, first_admin, OWNER, "the admin disabled every user")
        await db.commit()

    assert closed == 1
    assert (await _status_and_restriction(session_factory, first_user)) == (UserStatus.disabled, False)
    assert (await _status_and_restriction(session_factory, second_user)) == (UserStatus.disabled, True)


@pytest.mark.asyncio
async def test_enforcement_skips_a_user_whose_status_changed_after_the_check(session_factory):
    [(_, user_id)] = await _seed(session_factory)
    await _admin_sets_status(session_factory, user_id, UserStatus.disabled, close_restriction=False)

    pushed = AsyncMock()
    with patch.object(connection_limiter, "_push_to_nodes", new=pushed):
        async with session_factory() as db:
            await connection_limiter._enforce(db, [_Observation(user_id)], ENFORCING)

    async with session_factory() as db:
        restrictions = (await db.execute(select(ConnectionRestriction))).scalars().all()
    assert restrictions == []
    assert _pushed_ids(pushed) == []
