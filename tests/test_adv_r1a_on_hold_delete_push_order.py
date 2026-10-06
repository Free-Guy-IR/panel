import asyncio
from datetime import UTC, datetime as dt, timedelta as td
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select

from app import notification
from app.db.models import User, UserStatus
from app.fork.jobs import status_review
from app.jobs import review_users
from app.models.user import UserModify
from app.node import node_manager
from app.operation import OperatorType
from app.operation.user import UserOperation
from tests.a1_bridge_harness import RecordingNode, TestGetDB, isolate_node_manager, seed_group, seed_user, settle
from tests.api import TestSession
from tests.api.helpers import unique_name

OWNER = SimpleNamespace(username="owner", is_owner=True, role=None, id=None)
BRIDGE_DEFAULT_POLL_SECONDS = 1.0


async def test_an_on_hold_user_the_admin_extends_during_the_delete_pass_stays_on_the_node(monkeypatch):
    tag = unique_name("adv_hold")
    group_id = await seed_group(tag)
    user_id = await seed_user(
        group_id,
        status=UserStatus.on_hold,
        on_hold_expire_duration=86400,
        on_hold_timeout=dt.now(UTC) - td(minutes=5),
        created_at=dt.now(UTC) - td(days=2),
    )
    email = str(user_id)

    node = RecordingNode(2401)
    node._sync_poll_interval = BRIDGE_DEFAULT_POLL_SECONDS
    await node.connect("0.6.4", "26.3.27")
    node.live[email] = [tag]
    isolate_node_manager(monkeypatch, node_manager, {2401: node})
    monkeypatch.setattr(status_review, "GetDB", TestGetDB)
    monkeypatch.setattr(UserOperation, "generate_subscription_url", staticmethod(AsyncMock(return_value="")))
    for name in ("remove_user", "modify_user", "user_status_change"):
        monkeypatch.setattr(notification, name, AsyncMock())

    real_remove = review_users.sync_remove_user_awaited
    extended = []

    async def admin_extends_then_job_removes(response):
        if response.id == user_id and not extended:
            async with TestSession() as admin_db:
                await UserOperation(OperatorType.API).modify_user_by_id(
                    admin_db, user_id, UserModify(on_hold_timeout=dt.now(UTC) + td(days=3)), OWNER
                )
            extended.append(user_id)
            await asyncio.sleep(0.2)
        await real_remove(response)

    monkeypatch.setattr(review_users, "sync_remove_user_awaited", admin_extends_then_job_removes)

    await status_review.remove_on_hold_users_under_lock([user_id])
    await settle()
    await asyncio.sleep(BRIDGE_DEFAULT_POLL_SECONDS + 0.5)

    async with TestSession() as session:
        stored = (await session.execute(select(User.status).where(User.id == user_id))).scalar_one_or_none()
    assert extended == [user_id]
    assert stored == UserStatus.on_hold
    assert node.live.get(email), (
        f"user {user_id} was extended by an admin while the on-hold delete pass ran, so the pass kept it and pushed "
        f"it back directly; its earlier queued removal reached the node afterwards and the node now holds "
        f"{node.live.get(email)} (deliveries: {node.deliveries})"
    )
