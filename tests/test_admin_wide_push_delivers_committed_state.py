from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

import app.settings as settings_module
from app import notification
from app.db.crud.admin import get_admin_by_id
from app.db.crud.user import get_user_by_id
from app.db.models import User, UserStatus
from app.jobs import record_usages
from app.models.admin import AdminDetails, AdminRoleData
from app.models.user import UserStatusToggle
from app.node import bridge as node_bridge, core_users, sync as node_sync
from app.node.user import serialize_users_for_node
from app.operation import OperatorType, admin as admin_operation, user as user_operation
from app.operation.admin import AdminOperation
from app.operation.user import UserOperation
from tests.test_merge_interleavings_status_admin import _give_inbound
from tests.test_recorder_harness import _database_url, recorder_db_fixture, seed_users_and_nodes  # noqa: F401

pytestmark = pytest.mark.skipif(
    _database_url().startswith("sqlite"), reason="needs a server database with its own snapshot semantics"
)

OWNER = AdminDetails(id=None, username="owner", role=AdminRoleData(id=1, name="owner", is_owner=True))
BOUND = 120


@pytest.fixture(autouse=True)
async def quiet(monkeypatch: pytest.MonkeyPatch):
    stored = SimpleNamespace(
        telegram={}, webhook={}, notification_settings={}, notification_enable={}, subscription={}, hwid={}, general={}
    )
    monkeypatch.setattr(settings_module, "stored_settings", AsyncMock(return_value=stored))
    await settings_module.refresh_caches()
    monkeypatch.setattr(UserOperation, "generate_subscription_url", staticmethod(AsyncMock(return_value="")))
    for name in ("user_status_change", "modify_user", "reset_user_data_usage"):
        monkeypatch.setattr(notification, name, AsyncMock())
    yield
    await settings_module.refresh_caches()


@pytest.fixture(name="queued")
def queued_fixture(monkeypatch: pytest.MonkeyPatch, recorder_db) -> list[tuple[str, object]]:
    queued: list[tuple[str, object]] = []

    async def enqueue_many(source, users):
        queued.extend((source, payload) for payload in await serialize_users_for_node(users))

    async def admin_wide_push(users):
        await enqueue_many("admin-wide", users)

    async def single_push(db_user):
        await enqueue_many("single", [db_user])

    monkeypatch.setattr(admin_operation, "sync_users", admin_wide_push)
    monkeypatch.setattr(user_operation, "sync_user", single_push)
    monkeypatch.setattr(node_sync, "GetDB", record_usages.GetDB)
    monkeypatch.setattr(node_sync, "_current_state", node_sync.CurrentStateReader(query_timeout=BOUND))
    monkeypatch.setattr(node_bridge, "_refresh_handler", node_sync.refresh_node_users)
    return queued


async def _deliver(payload):
    (delivered,) = await asyncio.wait_for(
        node_bridge._QueuedBatchSync._current_state(SimpleNamespace(node_id="node-1"), [payload]), BOUND
    )
    return delivered


@pytest.mark.asyncio
async def test_a_stale_admin_wide_push_of_a_user_moved_out_and_re_enabled_is_delivered_as_the_committed_state(
    monkeypatch: pytest.MonkeyPatch, recorder_db, queued
):
    home, (moved_id, stay_id), _ = await seed_users_and_nodes(recorder_db, user_count=2, node_count=0)
    away, _, _ = await seed_users_and_nodes(recorder_db, user_count=0, node_count=0)
    await _give_inbound(recorder_db, [moved_id, stay_id])
    async with recorder_db() as session:
        await session.execute(update(User).where(User.id.in_([moved_id, stay_id])).values(status=UserStatus.active))
        await session.commit()

    real_visible = AdminOperation._visible_users_of
    real_by_ids = admin_operation.get_users_by_ids

    async def move_out_then_reread(db, db_admin, caller):
        async with recorder_db() as mover:
            await mover.execute(update(User).where(User.id == moved_id).values(admin_id=away))
            await mover.commit()
        return await real_visible(db, db_admin, caller)

    async def another_admin_enables_then_lookup(db, user_ids, **kwargs):
        if moved_id in user_ids:
            async with recorder_db() as other:
                db_user = await get_user_by_id(other, moved_id, load_admin_role=True)
                await UserOperation(OperatorType.API)._set_user_disabled(
                    other, db_user, UserStatusToggle(disabled=False), OWNER
                )
        return await real_by_ids(db, user_ids, **kwargs)

    monkeypatch.setattr(AdminOperation, "_visible_users_of", staticmethod(move_out_then_reread))
    monkeypatch.setattr(admin_operation, "get_users_by_ids", another_admin_enables_then_lookup)

    async with recorder_db() as db:
        db_admin = await get_admin_by_id(db, home)
        await AdminOperation(OperatorType.API)._disable_all_active_users_for_admin(db, db_admin, OWNER)

    async with recorder_db() as session:
        row = (await session.execute(select(User.admin_id, User.status).where(User.id == moved_id))).one()
        dialect = session.get_bind().dialect.name
    assert (row.admin_id, row.status) == (away, UserStatus.active)

    for_moved = [(source, payload) for source, payload in queued if payload.email == str(moved_id)]
    assert [source for source, _payload in for_moved] == ["single", "admin-wide"], for_moved
    last_payload = for_moved[-1][1]
    if dialect in ("mysql", "mariadb"):
        assert list(last_payload.inbounds) == [], (
            "the missing-user lookup no longer reuses the visibility read's snapshot, so this test lost its premise",
            for_moved,
        )

    delivered = await _deliver(last_payload)
    async with recorder_db() as session:
        (committed,) = await core_users(session, user_ids=[moved_id])
    assert delivered.email == str(moved_id)
    assert list(delivered.inbounds) == list(committed.inbounds) != [], (
        "the node received the stale queued payload instead of the state committed last",
        list(delivered.inbounds),
        list(committed.inbounds),
    )
