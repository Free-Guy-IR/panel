from __future__ import annotations

import asyncio
from contextlib import contextmanager
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, update

from app.db.crud.admin import get_admin_by_id
from app.db.crud.user import get_users_by_ids
from app.db.models import Admin, AdminRole, AdminStatus, User, UserStatus
from app.jobs import record_usages
from app.models.admin import AdminDetails, AdminRoleData
from app.node import sync as node_sync
from app.node.user import serialize_users_for_node
from app.operation import OperatorType, admin as admin_operation, admin_sync
from app.operation.admin import AdminOperation
from tests.test_merge_interleavings_status_admin import _give_inbound
from tests.test_recorder_harness import recorder_db_fixture, seed_users_and_nodes  # noqa: F401

OWNER = AdminDetails(id=None, username="owner", role=AdminRoleData(id=1, name="owner", is_owner=True))
SMALL = 2
LARGE = 7
REAL_PUSH = AdminOperation._push_admin_wide_change


@pytest.fixture(name="pushed")
def pushed_fixture(monkeypatch: pytest.MonkeyPatch) -> list:
    pushed: list = []

    async def record_dispatch(proto_users):
        pushed.extend(proto_users)

    monkeypatch.setattr(node_sync, "_dispatch_users_update", record_dispatch)
    monkeypatch.setattr(admin_operation.notification, "modify_admin", AsyncMock())
    return pushed


@contextmanager
def recorded_statements():
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    target = record_usages.engine.sync_engine
    event.listen(target, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(target, "before_cursor_execute", record)


async def _seed_admin(recorder_db, user_count: int, status: UserStatus) -> tuple[int, list[int]]:
    admin_id, user_ids, _ = await seed_users_and_nodes(recorder_db, user_count=user_count, node_count=0)
    await _give_inbound(recorder_db, user_ids)
    async with recorder_db() as session:
        for offset, user_id in enumerate(user_ids):
            await session.execute(update(User).where(User.id == user_id).values(status=status, hwid_limit=3 + offset))
        await session.commit()
    return admin_id, user_ids


async def _expected_payloads(recorder_db, user_ids: list[int]) -> dict[str, tuple]:
    async with recorder_db() as session:
        users = await get_users_by_ids(session, user_ids, load_admin_role=True)
        protos = await serialize_users_for_node(users)
    return {proto.email: _shape(proto) for proto in protos}


def _shape(proto) -> tuple:
    return proto.proxies.SerializeToString(deterministic=True), tuple(sorted(proto.inbounds))


async def _drain():
    for _ in range(5):
        await asyncio.sleep(0)


async def _block_transition_sync(recorder_db, pushed, user_count: int) -> tuple[list[str], list[int], dict]:
    admin_id, user_ids = await _seed_admin(recorder_db, user_count, UserStatus.active)
    async with record_usages.GetDB() as db:
        db_admin = await get_admin_by_id(db, admin_id)
        with recorded_statements() as statements:
            synced = await admin_sync.sync_admin_users_for_block_transition(db, db_admin, was_blocked=True)
    await _drain()
    assert synced == user_count
    return statements, user_ids, {proto.email: _shape(proto) for proto in pushed}


async def _block_transition_remove(recorder_db, pushed, user_count: int) -> tuple[list[str], list[int], dict]:
    admin_id, user_ids = await _seed_admin(recorder_db, user_count, UserStatus.active)
    async with recorder_db() as session:
        await session.execute(update(AdminRole).where(AdminRole.id == 3).values(disconnect_users_when_limited=True))
        await session.execute(update(Admin).where(Admin.id == admin_id).values(status=AdminStatus.limited))
        await session.commit()
    async with record_usages.GetDB() as db:
        db_admin = await get_admin_by_id(db, admin_id)
        with recorded_statements() as statements:
            removed = await admin_sync.sync_admin_users_for_block_transition(db, db_admin, was_blocked=False)
    await _drain()
    assert removed == user_count
    return statements, user_ids, {proto.email: proto for proto in pushed}


async def _activate_all(monkeypatch, recorder_db, pushed, user_count: int) -> tuple[list[str], list[int], dict]:
    admin_id, user_ids = await _seed_admin(recorder_db, user_count, UserStatus.disabled)
    captured: dict[str, list[str]] = {}

    async def counted_push(self, db, db_admin, admin, locked):
        with recorded_statements() as statements:
            await REAL_PUSH(self, db, db_admin, admin, locked)
        captured["statements"] = statements

    monkeypatch.setattr(AdminOperation, "_push_admin_wide_change", counted_push)
    async with record_usages.GetDB() as db:
        db_admin = await get_admin_by_id(db, admin_id)
        await AdminOperation(OperatorType.API)._activate_all_disabled_users_for_admin(db, db_admin, OWNER)
    await _drain()
    return captured["statements"], user_ids, {proto.email: _shape(proto) for proto in pushed}


def _reads_history(statements: list[str]) -> list[str]:
    return [statement for statement in statements if "user_usage_logs" in statement]


@pytest.mark.asyncio
@pytest.mark.parametrize("user_count", [SMALL, LARGE])
async def test_block_transition_sync_pushes_full_payloads_without_history_or_per_user_queries(
    recorder_db, pushed, user_count
):
    statements, user_ids, payloads = await _block_transition_sync(recorder_db, pushed, user_count)
    assert _reads_history(statements) == []
    assert payloads == await _expected_payloads(recorder_db, user_ids)
    assert all(inbounds for _proxies, inbounds in payloads.values())
    assert len(statements) <= 6, statements


@pytest.mark.asyncio
async def test_block_transition_sync_statement_count_does_not_grow_with_users(recorder_db, pushed):
    small, _, _ = await _block_transition_sync(recorder_db, pushed, SMALL)
    pushed.clear()
    large, _, _ = await _block_transition_sync(recorder_db, pushed, LARGE)
    assert len(large) == len(small), (small, large)


@pytest.mark.asyncio
async def test_block_transition_remove_keeps_each_users_connection_cap_without_history(recorder_db, pushed):
    statements, user_ids, payloads = await _block_transition_remove(recorder_db, pushed, LARGE)
    assert _reads_history(statements) == []
    expected = await _expected_payloads(recorder_db, user_ids)
    assert set(payloads) == set(expected)
    for email, proto in payloads.items():
        assert list(proto.inbounds) == []
        assert proto.proxies.SerializeToString(deterministic=True) == expected[email][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("user_count", [SMALL, LARGE])
async def test_admin_wide_activate_pushes_full_payloads_without_history_or_per_user_queries(
    monkeypatch: pytest.MonkeyPatch, recorder_db, pushed, user_count
):
    statements, user_ids, payloads = await _activate_all(monkeypatch, recorder_db, pushed, user_count)
    assert _reads_history(statements) == []
    assert payloads == await _expected_payloads(recorder_db, user_ids)
    assert all(inbounds for _proxies, inbounds in payloads.values())
    assert len(statements) <= 8, statements


@pytest.mark.asyncio
async def test_admin_wide_activate_statement_count_does_not_grow_with_users(
    monkeypatch: pytest.MonkeyPatch, recorder_db, pushed
):
    small, _, _ = await _activate_all(monkeypatch, recorder_db, pushed, SMALL)
    pushed.clear()
    large, _, _ = await _activate_all(monkeypatch, recorder_db, pushed, LARGE)
    assert len(large) == len(small), (small, large)
