from datetime import UTC, datetime as dt, timedelta as td
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import notification
from app.db.crud.user import get_user_by_id
from app.db.models import User, UserStatus
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


@pytest.mark.xfail(
    strict=True,
    reason="needs close_active_restriction called from UserOperation._set_user_disabled (routed to P6/P8)",
)
async def test_limiter_release_re_enables_a_user_the_admin_disabled_meanwhile(monkeypatch):
    tag = unique_name("a1_tag")
    group_id = await seed_group(tag)
    user_id = await seed_user(group_id, status=UserStatus.disabled)
    email = str(user_id)
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

    operation = UserOperation(operator_type=OperatorType.API)
    async with TestSession() as db:
        db_user = await get_user_by_id(db, user_id)
        answer = await operation._set_user_disabled(db, db_user, UserStatusToggle(disabled=True), ADMIN)
    assert answer.status == UserStatus.disabled

    async with TestGetDB() as db:
        await connection_limiter._release_due(db, None)
    await settle()

    async with TestSession() as session:
        status = (await session.get(User, user_id)).status
    assert status == UserStatus.disabled and email not in node.live, (
        f"the admin disabled user {email} while a device-limit restriction was running; when the restriction "
        f"expired the limiter set the status back to {status} and pushed inbounds {node.live.get(email)} to the node"
    )
