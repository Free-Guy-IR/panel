from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

import app.settings as settings_module
from app import notification
from app.db.crud.admin import get_admin_by_id
from app.db.models import User, UserStatus
from app.fork.crud.user_locks import USER_LOCK_BATCH_SIZE
from app.fork.models.connection import ConnectionRestriction
from app.models.admin import AdminDetails, AdminRoleData
from app.operation import OperatorType, admin as admin_operation
from app.operation.admin import AdminOperation
from app.operation.user import UserOperation
from tests.test_recorder_harness import recorder_db_fixture, seed_users_and_nodes  # noqa: F401

OWNER = AdminDetails(id=None, username="owner", role=AdminRoleData(id=1, name="owner", is_owner=True))
USERS = USER_LOCK_BATCH_SIZE + 23
ACTIONS = {
    "disable_all": ("_disable_all_active_users_for_admin", UserStatus.active, UserStatus.disabled),
    "activate_all": ("_activate_all_disabled_users_for_admin", UserStatus.disabled, UserStatus.active),
}


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


@pytest.mark.asyncio
@pytest.mark.parametrize("action", list(ACTIONS))
async def test_an_admin_wide_change_reaches_every_user_beyond_one_lock_batch(
    monkeypatch: pytest.MonkeyPatch, recorder_db, action
):
    method, before, after = ACTIONS[action]
    admin_id, user_ids, _ = await seed_users_and_nodes(recorder_db, user_count=USERS, node_count=0)
    async with recorder_db() as session:
        await session.execute(
            update(User)
            .where(User.id.in_(user_ids))
            .values(status=before, last_status_change=datetime.now(UTC) - timedelta(minutes=40))
        )
        if before == UserStatus.disabled:
            session.add_all(
                ConnectionRestriction(
                    user_id=user_id,
                    created_at=datetime.now(UTC) - timedelta(minutes=30),
                    previous_status=UserStatus.active.value,
                    disable_minutes=30,
                    restore_at=datetime.now(UTC) + timedelta(minutes=30),
                    active=True,
                )
                for user_id in user_ids
            )
        await session.commit()
    pushed: dict[int, UserStatus] = {}

    async def record_push(users):
        pushed.update((db_user.id, db_user.status) for db_user in users)

    monkeypatch.setattr(admin_operation, "sync_users", record_push)

    async with recorder_db() as db:
        db_admin = await get_admin_by_id(db, admin_id)
        await getattr(AdminOperation(OperatorType.API), method)(db, db_admin, OWNER)

    async with recorder_db() as session:
        statuses = dict((await session.execute(select(User.id, User.status).where(User.id.in_(user_ids)))).all())
        active = (
            (
                await session.execute(
                    select(ConnectionRestriction.user_id).where(
                        ConnectionRestriction.user_id.in_(user_ids), ConnectionRestriction.active.is_(True)
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(user_ids) > USER_LOCK_BATCH_SIZE
    assert [user_id for user_id in user_ids if statuses[user_id] != after] == []
    assert list(active) == []
    assert {user_id: pushed.get(user_id) for user_id in user_ids} == dict.fromkeys(user_ids, after)
