import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app import notification
from app.models.user import BulkUsersSelection, UserModify
from app.node import node_manager
from app.operation import OperatorType
from app.operation.user import UserOperation
from tests.a1_bridge_harness import RecordingNode, isolate_node_manager, seed_group, seed_user, settle
from tests.api import TestSession
from tests.api.helpers import unique_name

OWNER = SimpleNamespace(username="owner", is_owner=True, role=None, id=None)
BRIDGE_DEFAULT_POLL_SECONDS = 1.0


async def _fleet(monkeypatch):
    tag = unique_name("adv_del")
    group_id = await seed_group(tag)
    busy = await seed_user(group_id)
    edited = await seed_user(group_id)
    node = RecordingNode(2501)
    node._sync_poll_interval = BRIDGE_DEFAULT_POLL_SECONDS
    await node.connect("0.6.4", "26.3.27")
    node.live.update({str(busy): [tag], str(edited): [tag]})
    isolate_node_manager(monkeypatch, node_manager, {2501: node})
    monkeypatch.setattr(UserOperation, "generate_subscription_url", staticmethod(AsyncMock(return_value="")))
    for name in ("remove_user", "modify_user", "user_status_change"):
        monkeypatch.setattr(notification, name, AsyncMock())
    return node, busy, edited


async def _edit(user_id: int, note: str) -> None:
    async with TestSession() as db:
        await UserOperation(OperatorType.API).modify_user_by_id(db, user_id, UserModify(note=note), OWNER)


async def test_a_user_edited_then_deleted_does_not_come_back_on_the_node(monkeypatch):
    node, busy, edited = await _fleet(monkeypatch)
    await _edit(busy, "keeps the bridge worker busy")
    await asyncio.sleep(0.2)
    await _edit(edited, "edited right before the delete")
    async with TestSession() as db:
        await UserOperation(OperatorType.API).remove_user_by_id(db, edited, OWNER)
    await settle()
    await asyncio.sleep(BRIDGE_DEFAULT_POLL_SECONDS + 0.5)

    assert str(edited) not in node.live, (
        f"user {edited} was edited and then deleted; the node holds {node.live.get(str(edited))} after both pushes "
        f"landed (deliveries: {[d for d in node.deliveries if d[1] == str(edited)]})"
    )


async def test_a_user_edited_then_bulk_deleted_does_not_come_back_on_the_node(monkeypatch):
    node, busy, edited = await _fleet(monkeypatch)
    await _edit(busy, "keeps the bridge worker busy")
    await asyncio.sleep(0.2)
    await _edit(edited, "edited right before the delete")
    async with TestSession() as db:
        await UserOperation(OperatorType.API).bulk_remove_users(db, BulkUsersSelection(ids={edited}), OWNER)
    await settle()
    await asyncio.sleep(BRIDGE_DEFAULT_POLL_SECONDS + 0.5)

    assert str(edited) not in node.live, (
        f"user {edited} was edited and then bulk-deleted; the node holds {node.live.get(str(edited))} after both "
        f"pushes landed (deliveries: {[d for d in node.deliveries if d[1] == str(edited)]})"
    )


async def test_a_user_edited_just_before_the_expire_job_expires_it_is_not_left_on_the_node(monkeypatch):
    import importlib
    from datetime import UTC, datetime as dt, timedelta as td

    from sqlalchemy import update

    from app.db.models import User, UserStatus
    from app.jobs import review_users
    from tests.a1_bridge_harness import TestGetDB

    node, busy, edited = await _fleet(monkeypatch)
    monkeypatch.setattr(review_users, "GetDB", TestGetDB)
    try:
        status_review = importlib.import_module("app.fork.jobs.status_review")
        monkeypatch.setattr(status_review, "GetDB", TestGetDB)
    except ImportError:
        pass
    async with TestSession() as session:
        await session.execute(update(User).where(User.id == edited).values(expire=dt.now(UTC) - td(seconds=30)))
        await session.commit()

    await _edit(busy, "keeps the bridge worker busy")
    await asyncio.sleep(0.2)
    await _edit(edited, "edited moments after its expiry, before the job ran")
    await review_users.expire_users_job()
    await settle()
    await asyncio.sleep(BRIDGE_DEFAULT_POLL_SECONDS + 0.5)

    async with TestSession() as session:
        status = (await session.execute(select_status(edited))).scalar_one()
    assert status == UserStatus.expired
    assert str(edited) not in node.live, (
        f"user {edited} is {status.value} in the database but the node holds {node.live.get(str(edited))} "
        f"(deliveries: {[d for d in node.deliveries if d[1] == str(edited)]})"
    )


def select_status(user_id):
    from sqlalchemy import select

    from app.db.models import User

    return select(User.status).where(User.id == user_id)
