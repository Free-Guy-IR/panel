from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from app.core.manager import core_manager
from app.db.models import Group, ProxyInbound, User, UserStatus, users_groups_association
from app.models.proxy import ProxyTable
from app.node.user import core_users
from app.operation.node import NodeOperation
from tests.api import DATABASE_URL, TestSession
from tests.api.helpers import unique_name

pytestmark = pytest.mark.skipif(
    not DATABASE_URL.startswith("mysql"), reason="REPEATABLE READ snapshots only exist on the MySQL test database"
)


async def _seed(tag: str) -> int:
    async with TestSession() as session:
        group = Group(name=unique_name("rr_group"), inbounds=[ProxyInbound(tag=tag)])
        user = User(
            username=unique_name("rr_user"), proxy_settings=ProxyTable().dict(no_obj=True), status=UserStatus.active
        )
        session.add_all([group, user])
        await session.flush()
        await session.execute(users_groups_association.insert(), [{"user_id": user.id, "groups_id": group.id}])
        await session.commit()
        return user.id


async def test_a_lazy_snapshot_sees_a_change_committed_after_the_callers_first_read(monkeypatch):
    tag = unique_name("rr_tag")
    user_id = await _seed(tag)
    email = str(user_id)
    core = SimpleNamespace(inbounds=[tag], protocols=frozenset())
    monkeypatch.setattr(core_manager, "get_cores", AsyncMock(return_value={1: core}))

    async with TestSession() as caller:
        assert (await caller.scalar(select(User.status).where(User.id == user_id))) is UserStatus.active
        _, loaders = await NodeOperation._get_core_users_map(caller, {1}, lazy=True)

        async with TestSession() as admin:
            await admin.execute(update(User).where(User.id == user_id).values(status=UserStatus.disabled))
            await admin.commit()

        stale = {user.email for user in await core_users(caller, inbound_tags=[tag])}
        fresh = {user.email for user in await loaders[1]()}

    assert email in stale, "the caller's own transaction should still see its REPEATABLE READ snapshot"
    assert email not in fresh, "the deferred snapshot reused the caller's transaction and missed the disable"
