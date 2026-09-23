from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Admin, AdminRole, Base, Group, System, User, UserStatus
from app.models.admin import AdminDetails, AdminRoleData
from app.models.proxy import ProxyTable
from app.operation.system import SystemOperation

OWNER = AdminDetails(id=1, username="owner", role=AdminRoleData(id=1, name="owner", is_owner=True))


def _restricted(permissions: dict, allowed_group_ids: list[int]) -> AdminDetails:
    return AdminDetails(
        id=2,
        username="support",
        used_traffic=77,
        role=AdminRoleData(
            id=3,
            name="support",
            is_owner=False,
            permissions=permissions,
            access={"allowed_group_ids": allowed_group_ids},
        ),
    )


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as session:
        for role_id, name, is_owner in ((1, "owner", True), (3, "support", False)):
            role = AdminRole(name=name, is_owner=is_owner, permissions={}, limits={}, features={}, access={})
            role.id = role_id
            session.add(role)
        await session.flush()
        for admin_id, username, role_id, used in ((1, "owner", 1, 0), (2, "support", 3, 77), (3, "seller", 3, 9000)):
            admin = Admin(username=username, hashed_password="x", role_id=role_id)
            admin.id = admin_id
            admin.used_traffic = used
            session.add(admin)
        visible_group, hidden_group = Group(name="visible", inbounds=[]), Group(name="hidden", inbounds=[])
        session.add_all([visible_group, hidden_group])
        session.add(System(uplink=5000, downlink=6000))
        await session.flush()
        now = datetime.now(UTC)
        for username, groups, status, online in (
            ("seen_active", [visible_group], UserStatus.active, True),
            ("seen_disabled", [visible_group, hidden_group], UserStatus.disabled, False),
            ("hidden_active", [hidden_group], UserStatus.active, True),
            ("groupless_limited", [], UserStatus.limited, True),
        ):
            user = User(username=username, admin_id=3, proxy_settings=ProxyTable().dict(no_obj=True))
            user.groups = groups
            user.status = status
            user.online_at = now - timedelta(seconds=10) if online else None
            session.add(user)
        await session.commit()
        yield session, visible_group.id
    await engine.dispose()


def _counts(stats) -> tuple:
    return (
        stats.total_user,
        stats.online_users,
        stats.active_users,
        stats.disabled_users,
        stats.limited_users,
        stats.incoming_bandwidth,
        stats.outgoing_bandwidth,
        stats.admin_used_traffic,
    )


@pytest.mark.asyncio
async def test_the_owner_still_sees_every_user_and_the_system_bandwidth(db):
    session, _ = db

    stats = await SystemOperation.get_system_users_stats(session, OWNER)

    assert _counts(stats) == (4, 3, 2, 1, 1, 5000, 6000, None)


@pytest.mark.asyncio
async def test_a_group_restricted_admin_counts_only_users_inside_its_groups(db):
    session, visible_group_id = db
    support = _restricted({"users": {"read": True}}, [visible_group_id])

    stats = await SystemOperation.get_system_users_stats(session, support)

    assert _counts(stats) == (2, 1, 1, 1, 0, 0, 0, None)


@pytest.mark.asyncio
async def test_a_group_restricted_admin_reading_another_admin_still_counts_only_its_groups(db):
    session, visible_group_id = db
    support = _restricted({"users": {"read": True}, "admins": {"read": True}}, [visible_group_id])

    stats = await SystemOperation.get_system_users_stats(session, support, admin_username="seller")

    assert _counts(stats) == (2, 1, 1, 1, 0, 0, 0, None)


@pytest.mark.asyncio
async def test_a_role_with_an_empty_group_list_counts_nobody(db):
    session, _ = db
    support = _restricted({"users": {"read": True}}, [])

    stats = await SystemOperation.get_system_users_stats(session, support)

    assert _counts(stats) == (0, 0, 0, 0, 0, 0, 0, None)
