import asyncio
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.crud.admin import get_admin_by_id
from app.db.crud.user import get_user_by_id, modify_user
from app.db.models import Base, Group, ProxyInbound, User, UserStatus, users_groups_association
from app.fork.operation.user_extras import UserExtrasMixin
from app.models.admin import AdminDetails, AdminRoleData
from app.models.user import UserModify
from app.operation import OperatorType, admin as admin_operation
from app.operation.admin import AdminOperation
from tests.test_edit_rotation_probe import NEW_VLESS_ID, OWNER, STORED, _operation, _seed, _stored

ADMIN_OWNER = AdminDetails(id=None, username="owner", role=AdminRoleData(id=1, name="owner", is_owner=True))
BOUND = 30


@pytest_asyncio.fixture
async def sessions():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, autoflush=False, expire_on_commit=False)
    await engine.dispose()


async def _disable_every_user_of_the_owner(sessions) -> None:
    async with sessions() as db:
        user_admin_id = (await db.execute(select(User.admin_id))).scalars().first()
        db_admin = await get_admin_by_id(db, user_admin_id)
        await asyncio.wait_for(
            AdminOperation(OperatorType.API)._disable_all_active_users_for_admin(db, db_admin, ADMIN_OWNER), BOUND
        )


async def _add_a_second_group(sessions, user_id: int) -> int:
    async with sessions() as db:
        inbound = ProxyInbound(tag="vless-extra")
        group = Group(name="extra", inbounds=[inbound])
        db.add_all([inbound, group])
        await db.flush()
        await db.execute(users_groups_association.insert(), [{"user_id": user_id, "groups_id": group.id}])
        await db.commit()
        return group.id


async def _groups(sessions, user_id: int) -> set[int]:
    async with sessions() as db:
        rows = await db.execute(
            select(users_groups_association.c.groups_id).where(users_groups_association.c.user_id == user_id)
        )
        return set(rows.scalars().all())


@pytest.fixture(autouse=True)
def no_node_push(monkeypatch):
    monkeypatch.setattr(admin_operation, "sync_users", AsyncMock())


@pytest.mark.parametrize("cuts_the_limit", [False, True], ids=["secrets-only", "limit-cut"])
@pytest.mark.parametrize("races", [("disable",), ("group",), ("disable", "group"), ("group", "disable")])
@pytest.mark.asyncio
async def test_a_partial_edit_racing_an_admin_wide_disable_and_a_group_change_keeps_secrets_and_status(
    monkeypatch, sessions, races, cuts_the_limit
):
    user_id = await _seed(sessions)
    if cuts_the_limit:
        async with sessions() as db:
            await db.execute(update(User).where(User.id == user_id).values(used_traffic=100))
            await db.commit()
    async with sessions() as db:
        assert (await db.execute(select(User.status).where(User.id == user_id))).scalar_one() == UserStatus.active
    real_lock = UserExtrasMixin._lock_user_proxy_settings
    added: list[int] = []

    async def races_commit_then_lock(self, db, db_user, *args, **kwargs):
        for race in races:
            if race == "disable":
                await _disable_every_user_of_the_owner(sessions)
            else:
                added.append(await _add_a_second_group(sessions, user_id))
        return await real_lock(self, db, db_user, *args, **kwargs)

    monkeypatch.setattr(UserExtrasMixin, "_lock_user_proxy_settings", races_commit_then_lock)

    async with sessions() as db:
        db_user = await get_user_by_id(db, user_id, load_admin_role=True)
        before_groups = {group.id for group in db_user.groups}
        modified = UserModify(proxy_settings={"vless": {"id": NEW_VLESS_ID}}, data_limit=10 if cuts_the_limit else None)
        await asyncio.wait_for(
            _operation()._prepare_modified_user(db, db_user, modified, OWNER, skip_role_limits=True), BOUND
        )
        await asyncio.wait_for(modify_user(db, db_user, modified), BOUND)

    stored = await _stored(sessions, user_id)
    for protocol, (attribute, value) in STORED.items():
        assert stored[protocol][attribute] == value, (protocol, stored[protocol])
    assert stored["vless"]["id"] == NEW_VLESS_ID
    async with sessions() as db:
        status = (await db.execute(select(User.status).where(User.id == user_id))).scalar_one()
    if "disable" in races:
        assert status == UserStatus.disabled
    else:
        assert status == (UserStatus.limited if cuts_the_limit else UserStatus.active)
    assert await _groups(sessions, user_id) == before_groups | set(added)
