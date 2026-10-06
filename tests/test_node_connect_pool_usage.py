import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PasarGuardNodeBridge import Health
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool

from app.core.manager import core_manager
from app.db.models import NodeStatus, User
from app.node import manager_sync
from app.operation import OperatorType, node as node_operation
from app.operation.node import NodeOperation
from tests.api import DATABASE_URL, IS_SQLITE

pytestmark = pytest.mark.skipif(
    ":memory:" in DATABASE_URL, reason="every pooled connection must open the same database"
)

INFO = SimpleNamespace(started=True, node_version="0.9.0", core_version="26.3.27")


def _pool(pool_size: int, max_overflow: int):
    engine = create_async_engine(
        DATABASE_URL,
        poolclass=AsyncAdaptedQueuePool,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=1,
        connect_args={"check_same_thread": False} if IS_SQLITE else {},
    )
    return engine, async_sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _db_node(node_id: int, status=NodeStatus.error):
    return SimpleNamespace(id=node_id, name=f"pool{node_id}", status=status, core_config_id=1, keep_alive=0)


async def _lookup(db, node_id, status=NodeStatus.error):
    await db.execute(select(User.id).limit(1))
    return _db_node(node_id, status)


def _install(monkeypatch, node_ids, *, before_start_phase=None):
    core = SimpleNamespace(
        type=None, inbounds=["pool_usage_tag"], protocols=frozenset(), exclude_inbound_tags=[], to_str=lambda: "{}"
    )
    monkeypatch.setattr(core_manager, "get_cores", AsyncMock(return_value={1: core}))
    pg_nodes = {
        node_id: SimpleNamespace(
            get_health=AsyncMock(return_value=Health.NOT_CONNECTED),
            get_lifecycle_state=AsyncMock(return_value=None),
            start=AsyncMock(return_value=INFO),
            stop=AsyncMock(),
            sync_users=AsyncMock(),
        )
        for node_id in node_ids
    }

    async def update_node(db_node):
        if before_start_phase is not None:
            await before_start_phase()
        return pg_nodes[db_node.id]

    monkeypatch.setattr(node_operation.node_manager, "update_node", update_node)
    monkeypatch.setattr(node_operation.node_manager, "get_node", AsyncMock(side_effect=lambda i: pg_nodes[i]))
    monkeypatch.setattr(node_operation, "get_node_by_id", lambda db, node_id, **_: _lookup(db, node_id))
    monkeypatch.setattr(node_operation, "update_node_status", AsyncMock())
    monkeypatch.setattr(node_operation, "bulk_update_node_status", AsyncMock())
    monkeypatch.setattr(node_operation.notification, "connect_node", AsyncMock())
    monkeypatch.setattr(node_operation.notification, "error_node", AsyncMock())
    return pg_nodes


async def test_a_single_connect_starts_with_one_pooled_connection(monkeypatch):
    pg_nodes = _install(monkeypatch, [1])
    engine, sessions = _pool(1, 0)
    try:
        async with sessions() as db:
            assert await NodeOperation(operator_type=OperatorType.SYSTEM)._connect_single_node_local(db, 1)
    finally:
        await engine.dispose()
    pg_nodes[1].start.assert_awaited_once()


async def test_a_bulk_connect_starts_with_one_pooled_connection(monkeypatch):
    pg_nodes = _install(monkeypatch, [1, 2, 3])
    engine, sessions = _pool(1, 0)
    try:
        async with sessions() as db:
            nodes = [await _lookup(db, node_id) for node_id in pg_nodes]
            await NodeOperation(operator_type=OperatorType.SYSTEM)._connect_nodes_bulk_local(
                db, nodes, force_start=True
            )
    finally:
        await engine.dispose()
    assert all(node.start.await_count == 1 for node in pg_nodes.values())
    node_operation.bulk_update_node_status.assert_awaited_once()


async def test_a_user_sync_reads_with_one_pooled_connection(monkeypatch):
    pg_nodes = _install(monkeypatch, [1])
    monkeypatch.setattr(
        NodeOperation,
        "get_validated_node",
        lambda self, db, node_id, **_: _lookup(db, node_id, NodeStatus.connected),
    )
    monkeypatch.setattr(node_operation, "NodeResponse", SimpleNamespace(model_validate=lambda node: node))
    engine, sessions = _pool(1, 0)
    try:
        async with sessions() as db:
            await NodeOperation(operator_type=OperatorType.SYSTEM)._sync_node_users_local(db, 1, flush_users=False)
    finally:
        await engine.dispose()
    pg_nodes[1].sync_users.assert_awaited_once()


async def test_a_sibling_connect_starts_with_one_pooled_connection(monkeypatch):
    pg_nodes = _install(monkeypatch, [1])
    engine, sessions = _pool(1, 0)
    monkeypatch.setattr(manager_sync, "GetDB", sessions)
    monkeypatch.setattr(
        manager_sync, "get_node_by_id", lambda db, node_id, **_: _lookup(db, node_id, NodeStatus.connected)
    )
    try:
        await manager_sync.handle_node_message({"action": "connect", "node_id": 1, "origin": "another-worker"})
    finally:
        await engine.dispose()
    pg_nodes[1].start.assert_awaited_once()


async def test_as_many_concurrent_connects_as_the_pool_holds_all_start(monkeypatch):
    connects = 10
    everyone_looked_up = asyncio.Barrier(connects)
    pg_nodes = _install(monkeypatch, list(range(1, connects + 1)), before_start_phase=everyone_looked_up.wait)
    engine, sessions = _pool(5, 5)
    operation = NodeOperation(operator_type=OperatorType.SYSTEM)

    async def connect(node_id):
        async with sessions() as db:
            return await operation._connect_single_node_local(db, node_id)

    try:
        async with asyncio.timeout(20):
            results = await asyncio.gather(*(connect(node_id) for node_id in pg_nodes), return_exceptions=True)
    finally:
        await engine.dispose()
    assert [result for result in results if result is not True] == []
    assert all(node.start.await_count == 1 for node in pg_nodes.values())


async def test_concurrent_eager_reads_at_pool_size_need_no_second_connection(monkeypatch):
    callers = 3
    core = SimpleNamespace(inbounds=["pool_usage_tag"], protocols=frozenset())
    monkeypatch.setattr(core_manager, "get_cores", AsyncMock(return_value={1: core}))
    everyone_holds = asyncio.Barrier(callers)
    engine, sessions = _pool(callers, 0)

    async def eager_read():
        async with sessions() as db:
            await db.execute(select(User.id).limit(1))
            await everyone_holds.wait()
            _, users_by_core = await NodeOperation._get_core_users_map(db, {1})
            return users_by_core[1]

    try:
        async with asyncio.timeout(20):
            results = await asyncio.gather(*(eager_read() for _ in range(callers)), return_exceptions=True)
    finally:
        await engine.dispose()
    assert [result for result in results if isinstance(result, BaseException)] == []
