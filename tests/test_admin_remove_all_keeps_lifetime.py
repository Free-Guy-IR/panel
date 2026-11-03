from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import update

from app.db.crud.admin import get_admin_by_id
from app.db.models import User, UserStatus, UserUsageResetLogs
from app.models.admin import AdminDetails, AdminRoleData
from app.node import sync as node_sync
from app.operation import OperatorType, admin as admin_operation
from app.operation.admin import AdminOperation
from app.operation.user import UserOperation
from tests.test_merge_interleavings_status_admin import _give_inbound
from tests.test_recorder_harness import recorder_db_fixture, seed_users_and_nodes  # noqa: F401

OWNER = AdminDetails(id=None, username="owner", role=AdminRoleData(id=1, name="owner", is_owner=True))


@pytest.mark.asyncio
async def test_remove_all_users_webhook_keeps_lifetime_traffic(recorder_db, monkeypatch):
    async def record_dispatch(proto_users):
        return None

    monkeypatch.setattr(node_sync, "_dispatch_users_update", record_dispatch)
    removed = AsyncMock()
    monkeypatch.setattr(admin_operation.notification, "remove_user", removed)
    monkeypatch.setattr(UserOperation, "generate_subscription_url", AsyncMock(return_value=""))

    admin_id, user_ids, _ = await seed_users_and_nodes(recorder_db, user_count=2, node_count=0)
    await _give_inbound(recorder_db, user_ids)
    async with recorder_db() as session:
        await session.execute(
            update(User).where(User.id.in_(user_ids)).values(status=UserStatus.active, used_traffic=5)
        )
        session.add(UserUsageResetLogs(user_id=user_ids[0], used_traffic_at_reset=1000))
        await session.commit()

    async with recorder_db() as session:
        db_admin = await get_admin_by_id(session, admin_id)
        deleted = await AdminOperation(OperatorType.API)._remove_all_users_for_admin(session, db_admin, OWNER)

    for _ in range(5):
        await asyncio.sleep(0)
    assert deleted == 2
    lifetimes = {call.args[0].id: call.args[0].lifetime_used_traffic for call in removed.call_args_list}
    assert lifetimes == {user_ids[0]: 1005, user_ids[1]: 5}
