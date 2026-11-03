from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import update

from app.db.crud.admin import get_admin_by_id
from app.db.models import Admin, AdminRole, AdminStatus, User, UserStatus
from app.jobs import record_usages
from app.models.admin import BulkAdminSelection
from app.models.proxy import ProxyTable
from app.node import sync as node_sync
from app.operation import OperatorType, admin as admin_operation, admin_sync
from app.operation.admin import AdminOperation
from tests.test_admin_node_sync_load import OWNER, _drain, _expected_payloads, _seed_admin, _shape
from tests.test_recorder_harness import recorder_db_fixture, seed_users_and_nodes  # noqa: F401


@pytest.fixture(name="pushed")
def pushed_fixture(monkeypatch: pytest.MonkeyPatch) -> list:
    pushed: list = []

    async def record_dispatch(proto_users):
        pushed.extend(proto_users)

    monkeypatch.setattr(node_sync, "_dispatch_users_update", record_dispatch)
    monkeypatch.setattr(admin_operation.notification, "modify_admin", AsyncMock())
    return pushed


async def _modify_user(recorder_db, user_id: int, status: UserStatus) -> None:
    async with recorder_db() as session:
        await session.execute(
            update(User)
            .where(User.id == user_id)
            .values(status=status, hwid_limit=41, proxy_settings=ProxyTable().dict(no_obj=True))
        )
        await session.commit()


def _last_pushed(pushed: list) -> dict:
    last: dict = {}
    for proto in pushed:
        last[proto.email] = _shape(proto)
    return last


@pytest.mark.asyncio
@pytest.mark.parametrize("racing_status", [UserStatus.active, UserStatus.disabled])
async def test_block_transition_sync_pushes_a_user_modify_that_commits_before_its_load(
    monkeypatch: pytest.MonkeyPatch, recorder_db, pushed, racing_status
):
    admin_id, user_ids = await _seed_admin(recorder_db, 3, UserStatus.active)
    other_admin_id, other_ids = await _seed_admin(recorder_db, 2, UserStatus.active)
    racing = user_ids[1]
    real_loader = admin_sync.get_admin_users_for_node_sync

    async def modify_then_load(db, loaded_admin_id, **kwargs):
        await _modify_user(recorder_db, racing, racing_status)
        return await real_loader(db, loaded_admin_id, **kwargs)

    monkeypatch.setattr(admin_sync, "get_admin_users_for_node_sync", modify_then_load)
    async with record_usages.GetDB() as db:
        db_admin = await db.get(Admin, admin_id)
        await admin_sync.sync_admin_users_for_block_transition(db, db_admin, was_blocked=True)
    await _drain()

    last = _last_pushed(pushed)
    assert set(last) == {str(user_id) for user_id in user_ids}
    assert not set(last) & {str(user_id) for user_id in other_ids}
    assert last == await _expected_payloads(recorder_db, user_ids)
    assert other_admin_id != admin_id


def _modify_before_the_node_sync_load(monkeypatch: pytest.MonkeyPatch, recorder_db, racing: int, status) -> None:
    real_loader = admin_sync.get_admin_users_for_node_sync

    async def modify_then_load(db, loaded_admin_id, **kwargs):
        await _modify_user(recorder_db, racing, status)
        return await real_loader(db, loaded_admin_id, **kwargs)

    monkeypatch.setattr(admin_sync, "get_admin_users_for_node_sync", modify_then_load)


async def _block_admin_role(recorder_db, admin_id: int, status: AdminStatus) -> None:
    async with recorder_db() as session:
        await session.execute(
            update(AdminRole)
            .where(AdminRole.id == 3)
            .values(disconnect_users_when_limited=True, disconnect_users_when_disabled=True)
        )
        await session.execute(update(Admin).where(Admin.id == admin_id).values(status=status))
        await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("racing_status", [UserStatus.active, UserStatus.disabled])
async def test_bulk_enable_pushes_a_user_modify_that_commits_after_the_admins_users_were_loaded(
    monkeypatch: pytest.MonkeyPatch, recorder_db, pushed, racing_status
):
    admin_id, user_ids = await _seed_admin(recorder_db, 3, UserStatus.active)
    _, other_ids = await _seed_admin(recorder_db, 2, UserStatus.active)
    await _block_admin_role(recorder_db, admin_id, AdminStatus.disabled)
    _modify_before_the_node_sync_load(monkeypatch, recorder_db, user_ids[2], racing_status)

    async with record_usages.GetDB() as db:
        await AdminOperation(OperatorType.API).bulk_set_admins_disabled(
            db, BulkAdminSelection(ids={admin_id}), OWNER, is_disabled=False
        )
    await _drain()

    last = _last_pushed(pushed)
    assert set(last) == {str(user_id) for user_id in user_ids}
    assert not set(last) & {str(user_id) for user_id in other_ids}
    assert last == await _expected_payloads(recorder_db, user_ids)


@pytest.mark.asyncio
async def test_block_transition_remove_drops_a_user_reactivated_before_its_load(
    monkeypatch: pytest.MonkeyPatch, recorder_db, pushed
):
    admin_id, user_ids = await _seed_admin(recorder_db, 3, UserStatus.active)
    racing = user_ids[0]
    await _modify_user(recorder_db, racing, UserStatus.disabled)
    await _block_admin_role(recorder_db, admin_id, AdminStatus.limited)
    _modify_before_the_node_sync_load(monkeypatch, recorder_db, racing, UserStatus.active)

    async with record_usages.GetDB() as db:
        db_admin = await db.get(Admin, admin_id)
        removed = await admin_sync.sync_admin_users_for_block_transition(db, db_admin, was_blocked=False)
    await _drain()

    assert removed == 3
    assert {proto.email for proto in pushed} == {str(user_id) for user_id in user_ids}
    assert all(list(proto.inbounds) == [] for proto in pushed)


@pytest.mark.asyncio
@pytest.mark.parametrize("racing_status", [UserStatus.active, UserStatus.disabled])
async def test_admin_wide_activate_pushes_a_user_modify_that_commits_between_the_write_and_the_push_load(
    monkeypatch: pytest.MonkeyPatch, recorder_db, pushed, racing_status
):
    admin_id, user_ids = await _seed_admin(recorder_db, 3, UserStatus.disabled)
    _, other_ids = await _seed_admin(recorder_db, 2, UserStatus.disabled)
    racing = user_ids[0]
    real_visible = AdminOperation._visible_users_of

    async def modify_then_read(db, db_admin, caller):
        await db.commit()
        await _modify_user(recorder_db, racing, racing_status)
        return await real_visible(db, db_admin, caller)

    monkeypatch.setattr(AdminOperation, "_visible_users_of", staticmethod(modify_then_read))
    async with record_usages.GetDB() as db:
        db_admin = await get_admin_by_id(db, admin_id)
        await AdminOperation(OperatorType.API)._activate_all_disabled_users_for_admin(db, db_admin, OWNER)
    await _drain()

    last = _last_pushed(pushed)
    assert set(last) == {str(user_id) for user_id in user_ids}
    assert not set(last) & {str(user_id) for user_id in other_ids}
    assert last == await _expected_payloads(recorder_db, user_ids)
