from datetime import UTC, datetime as dt, timedelta as td
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import notification
from app.db.crud.user import get_user_by_id
from app.db.models import Admin, User, UserStatus
from app.fork.jobs import connection_limiter
from app.fork.models.connection import ConnectionRestriction
from app.models.user import UserStatusToggle
from app.node import node_manager
from app.operation import OperatorType
from app.operation.user import UserOperation
from tests.a1_bridge_harness import RecordingNode, TestGetDB, isolate_node_manager, seed_group, seed_user, settle
from tests.api import TestSession
from tests.api.helpers import unique_name

ADMIN = SimpleNamespace(username="owner", is_owner=True, role=None)


async def _restricted_user_on_a_node(monkeypatch, **fields) -> tuple[int, RecordingNode]:
    tag = unique_name("a1_tag")
    group_id = await seed_group(tag)
    user_id = await seed_user(group_id, status=UserStatus.disabled, **fields)
    async with TestSession() as session:
        session.add(
            ConnectionRestriction(
                user_id=user_id,
                previous_status=UserStatus.active.value,
                disable_minutes=30,
                restore_at=dt.now(UTC) - td(seconds=1),
                active=True,
            )
        )
        await session.commit()

    node = RecordingNode(1001)
    await node.connect("0.6.4", "26.3.27")
    isolate_node_manager(monkeypatch, node_manager, {1001: node})
    monkeypatch.setattr(connection_limiter, "GetDB", TestGetDB)
    monkeypatch.setattr(UserOperation, "generate_subscription_url", staticmethod(AsyncMock(return_value="")))
    for name in ("user_status_change", "modify_user"):
        monkeypatch.setattr(notification, name, AsyncMock())
    return user_id, node


async def _release_and_read(user_id: int, node: RecordingNode) -> tuple[UserStatus, list | None]:
    async with TestGetDB() as db:
        await connection_limiter._release_due(db, None)
    await settle()
    async with TestSession() as session:
        status = (await session.get(User, user_id)).status
    return status, node.live.get(str(user_id))


async def test_limiter_release_re_enables_a_user_the_admin_disabled_meanwhile(monkeypatch):
    user_id, node = await _restricted_user_on_a_node(monkeypatch)

    operation = UserOperation(operator_type=OperatorType.API)
    async with TestSession() as db:
        db_user = await get_user_by_id(db, user_id)
        answer = await operation._set_user_disabled(db, db_user, UserStatusToggle(disabled=True), ADMIN)
    assert answer.status == UserStatus.disabled

    status, live = await _release_and_read(user_id, node)
    assert status == UserStatus.disabled and live is None, (
        f"the admin disabled user {user_id} while a device-limit restriction was running; when the restriction "
        f"expired the limiter set the status back to {status} and pushed inbounds {live} to the node"
    )


@pytest.mark.xfail(
    strict=True,
    reason="the admin-wide call site in AdminOperation._disable_all_active_users_for_admin is wired by P8 (f9af373de)",
)
async def test_limiter_release_re_enables_a_user_whose_admin_disabled_every_user_meanwhile(monkeypatch):
    from app.db.crud.admin import get_admin_by_id
    from app.operation.admin import AdminOperation

    async with TestSession() as session:
        owner_admin = Admin(username=unique_name("a1_admin"), hashed_password="x", role_id=3)
        session.add(owner_admin)
        await session.commit()
        admin_id = owner_admin.id
    user_id, node = await _restricted_user_on_a_node(monkeypatch, admin_id=admin_id)

    async with TestSession() as db:
        db_admin = await get_admin_by_id(db, admin_id)
        await AdminOperation(OperatorType.API)._disable_all_active_users_for_admin(db, db_admin, ADMIN)

    status, live = await _release_and_read(user_id, node)
    assert status == UserStatus.disabled and live is None, (
        f"the admin disabled every user of admin {admin_id} while user {user_id} was restricted; when the "
        f"restriction expired the limiter set the status back to {status} and pushed inbounds {live} to the node"
    )


async def test_an_admin_disable_that_races_the_limiter_release_leaves_the_user_disabled_everywhere(monkeypatch):
    user_id, node = await _restricted_user_on_a_node(monkeypatch)
    email = str(user_id)

    operation = UserOperation(operator_type=OperatorType.API)
    async with TestSession() as db:
        db_user = await get_user_by_id(db, user_id)
        assert db_user.status == UserStatus.disabled

        async with TestGetDB() as limiter_db:
            await connection_limiter._release_due(limiter_db, None)
        await settle()
        assert email in node.live, "the limiter release did not put the user back on the node"

        answer = await operation._set_user_disabled(db, db_user, UserStatusToggle(disabled=True), ADMIN)
    await settle()

    async with TestSession() as session:
        stored = (await session.get(User, user_id)).status
    assert (answer.status, stored, node.live.get(email)) == (UserStatus.disabled, UserStatus.disabled, None), (
        f"the admin disabled user {email} after the limiter had already restored it: the API answered "
        f"{answer.status}, the database holds {stored}, the node holds {node.live.get(email)}"
    )


async def test_an_admin_enable_that_races_the_limiter_release_answers_and_pushes_what_the_database_holds(
    monkeypatch,
):
    user_id, node = await _restricted_user_on_a_node(monkeypatch)
    email = str(user_id)

    operation = UserOperation(operator_type=OperatorType.API)
    async with TestSession() as db:
        db_user = await get_user_by_id(db, user_id)
        async with TestGetDB() as limiter_db:
            await connection_limiter._release_due(limiter_db, None)
        await settle()

        answer = await operation._set_user_disabled(db, db_user, UserStatusToggle(disabled=False), ADMIN)
    await settle()

    async with TestSession() as session:
        stored = (await session.get(User, user_id)).status
    assert (answer.status, stored) == (UserStatus.active, UserStatus.active)
    assert node.live.get(email), f"user {email} is active in the database but missing from the node"
