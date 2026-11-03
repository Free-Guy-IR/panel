import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool

from app.db import base
from app.db.models import Node
from app.models.node import BulkNodeSelection, NodeCoreUpdate, NodeGeoFilesUpdate
from app.operation import OperatorType, node as node_operation_module
from app.operation.node import NodeOperation

POOL_SIZE = 2


@pytest.fixture
async def bounded_db(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'maintenance.db'}",
        poolclass=AsyncAdaptedQueuePool,
        pool_size=POOL_SIZE,
        max_overflow=0,
        pool_timeout=1,
    )
    async with engine.begin() as conn:
        await conn.run_sync(base.Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as session:
        for index in range(1, 4):
            session.add(
                Node(
                    name=f"n{index}",
                    address=f"10.0.0.{index}",
                    port=1000 + index,
                    api_port=2000 + index,
                    server_ca="ca",
                    api_key="key",
                    core_config_id=None,
                )
            )
        await session.commit()
    yield engine, factory
    await engine.dispose()


async def _ordinary_request(factory):
    async with factory() as session:
        return (await session.execute(text("select 1"))).scalar_one()


def _maintenance_calls(operation, db):
    return {
        "update_node": lambda: operation.update_node(db, 1),
        "update_core": lambda: operation.update_core(db, 1, NodeCoreUpdate(core_version="v1.2.3")),
        "update_geofiles": lambda: operation.update_geofiles(db, 1, NodeGeoFilesUpdate()),
    }


class _Blocker:
    def __init__(self, expected):
        self.expected = expected
        self.entered = 0
        self.all_entered = asyncio.Event()
        self.release = asyncio.Event()

    async def wait(self):
        self.entered += 1
        if self.entered >= self.expected:
            self.all_entered.set()
        await self.release.wait()


async def _run_concurrently(engine, factory, operation, action, blocker):
    sessions = [factory() for _ in range(POOL_SIZE)]
    tasks = [asyncio.create_task(_maintenance_calls(operation, db)[action]()) for db in sessions]
    try:
        await asyncio.wait_for(blocker.all_entered.wait(), 5)
        checked_out_while_waiting = engine.pool.checkedout()
        ordinary = await asyncio.wait_for(_ordinary_request(factory), 5)
    finally:
        blocker.release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for db in sessions:
            await db.close()
    return checked_out_while_waiting, ordinary, results


@pytest.mark.parametrize("action", ["update_node", "update_core", "update_geofiles"])
async def test_remote_maintenance_wait_holds_no_db_connection(bounded_db, monkeypatch, action):
    engine, factory = bounded_db
    blocker = _Blocker(POOL_SIZE)

    async def blocking_request(rpc_action, payload, timeout=None):
        await blocker.wait()
        return {"action": rpc_action}

    monkeypatch.setattr(node_operation_module.node_nats_client, "request", blocking_request)
    operation = NodeOperation(OperatorType.API)
    operation._update_node_api_impl = operation._update_node_api_remote
    operation._update_core_impl = operation._update_core_remote
    operation._update_geofiles_impl = operation._update_geofiles_remote

    checked_out, ordinary, results = await _run_concurrently(engine, factory, operation, action, blocker)

    assert checked_out == 0
    assert ordinary == 1
    assert all(isinstance(result, dict) for result in results), results


@pytest.mark.parametrize("action", ["update_node", "update_core", "update_geofiles"])
async def test_local_maintenance_wait_holds_no_db_connection(bounded_db, monkeypatch, action):
    engine, factory = bounded_db
    blocker = _Blocker(POOL_SIZE)

    async def blocking_call(*args):
        await blocker.wait()
        return SimpleNamespace(json=lambda: {"ok": True})

    bridge_node = SimpleNamespace(update_node=blocking_call, update_core=blocking_call, update_geofiles=blocking_call)

    async def get_node(node_id):
        return bridge_node

    monkeypatch.setattr(node_operation_module.node_manager, "get_node", get_node)
    operation = NodeOperation(OperatorType.API)
    operation._update_node_api_impl = operation._update_node_api_local
    operation._update_core_impl = operation._update_core_local
    operation._update_geofiles_impl = operation._update_geofiles_local

    checked_out, ordinary, results = await _run_concurrently(engine, factory, operation, action, blocker)

    assert checked_out == 0
    assert ordinary == 1
    assert results == [{"ok": True}] * POOL_SIZE


async def test_bulk_update_holds_no_connection_and_keeps_concurrent_rename(bounded_db, monkeypatch):
    engine, factory = bounded_db
    checked_out_per_call = []
    first_call_waiting = asyncio.Event()
    release = asyncio.Event()

    async def blocking_request(rpc_action, payload, timeout=None):
        checked_out_per_call.append(engine.pool.checkedout())
        first_call_waiting.set()
        await release.wait()
        return {}

    monkeypatch.setattr(node_operation_module.node_nats_client, "request", blocking_request)
    operation = NodeOperation(OperatorType.API)
    operation._update_node_api_impl = operation._update_node_api_remote

    expiring_factory = async_sessionmaker(bind=engine, expire_on_commit=True)
    async with expiring_factory() as bulk_db:
        bulk = asyncio.create_task(
            operation.bulk_update_nodes(bulk_db, BulkNodeSelection(ids={1, 2, 3}), SimpleNamespace(username="root"))
        )
        try:
            await asyncio.wait_for(first_call_waiting.wait(), 5)
            checked_out_while_waiting = engine.pool.checkedout()
            async with factory() as writer:
                await writer.execute(update(Node).where(Node.id == 2).values(name="renamed-meanwhile"))
                await asyncio.wait_for(writer.commit(), 5)
            ordinary = await asyncio.wait_for(_ordinary_request(factory), 5)
        finally:
            release.set()
            response = await bulk

    async with factory() as reader:
        stored_name = (await reader.execute(select(Node.name).where(Node.id == 2))).scalar_one()

    assert checked_out_while_waiting == 0
    assert checked_out_per_call == [0, 0, 0]
    assert ordinary == 1
    assert stored_name == "renamed-meanwhile"
    assert response.count == 3
    assert sorted(response.nodes) == ["n1", "n2", "n3"]
