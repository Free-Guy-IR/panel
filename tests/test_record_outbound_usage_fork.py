from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError

from app.db.crud.node import remove_node, reset_node_usage
from app.db.models import Node, NodeOutboundUsage, NodeUsage, NodeUsageResetLogs, System
from app.jobs import record_usages
from app.models.settings import General, UsageSettings
from tests import test_record_usages as recorder_tests

DummyNode = recorder_tests.DummyNode
session_factory = recorder_tests.session_factory


async def _seed_node(session_factory) -> int:
    async with session_factory() as session:
        node = Node(
            name="outbound-node",
            address="10.0.0.1",
            port=1000,
            api_port=1001,
            server_ca="ca1",
            api_key="key1",
            core_config_id=None,
        )
        session.add_all([node, System(uplink=0, downlink=0)])
        await session.flush()
        node_id = node.id
        await session.commit()
    return node_id


def _patch_job(monkeypatch: pytest.MonkeyPatch, node_id: int, polls: list, settings_reads: list) -> None:
    monkeypatch.setattr(record_usages, "_retained_node_usage", [])
    monkeypatch.setattr(
        record_usages.node_manager, "get_healthy_nodes", AsyncMock(return_value=[(node_id, DummyNode(node_id))])
    )

    async def fake_get_outbounds_stats(node: DummyNode, node_id: int | None = None):
        poll = polls.pop(0)
        return await poll() if callable(poll) else poll

    async def counting_general_settings():
        settings_reads.append(1)
        return General(usage=UsageSettings(record_node_usage=True, record_outbound_usage=True))

    monkeypatch.setattr(record_usages, "get_outbounds_stats", fake_get_outbounds_stats)
    monkeypatch.setattr(record_usages, "general_settings", counting_general_settings)


async def _totals(session_factory, node_id: int):
    async with session_factory() as session:
        node = (await session.execute(select(Node.uplink, Node.downlink).where(Node.id == node_id))).one()
        outbound = (
            await session.execute(
                select(
                    NodeOutboundUsage.outbound_tag,
                    func.sum(NodeOutboundUsage.uplink),
                    func.sum(NodeOutboundUsage.downlink),
                )
                .where(NodeOutboundUsage.node_id == node_id)
                .group_by(NodeOutboundUsage.outbound_tag)
            )
        ).all()
        node_history = (
            await session.execute(
                select(func.coalesce(func.sum(NodeUsage.uplink), 0), func.coalesce(func.sum(NodeUsage.downlink), 0))
            )
        ).one()
        logged = (
            await session.execute(
                select(
                    func.coalesce(func.sum(NodeUsageResetLogs.uplink), 0),
                    func.coalesce(func.sum(NodeUsageResetLogs.downlink), 0),
                ).where(NodeUsageResetLogs.node_id == node_id)
            )
        ).one()
    return tuple(node), {tag: (up, down) for tag, up, down in outbound}, tuple(node_history), tuple(logged)


@pytest.mark.asyncio
async def test_a_retried_write_counts_outbound_rows_once(monkeypatch: pytest.MonkeyPatch, session_factory):
    node_id = await _seed_node(session_factory)
    sample = [
        {"tag": "direct", "up": 5, "down": 0},
        {"tag": "direct", "up": 0, "down": 6},
        {"tag": "warp", "up": 1, "down": 2},
    ]
    settings_reads: list = []
    _patch_job(monkeypatch, node_id, [sample], settings_reads)
    real_run = record_usages.run_in_retried_transaction
    attempts = {"n": 0}

    async def fail_once_after_the_writes(operation, *args, **kwargs):
        async def write_then_fail(conn):
            result = await operation(conn)
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise OperationalError("COMMIT", {}, Exception("database is locked"))
            return result

        return await real_run(write_then_fail, *args, **kwargs)

    monkeypatch.setattr(record_usages, "run_in_retried_transaction", fail_once_after_the_writes)
    await record_usages.record_node_usages()

    node, outbound, node_history, _ = await _totals(session_factory, node_id)
    assert attempts["n"] == 2
    assert outbound == {"direct": (5, 6), "warp": (1, 2)}
    assert node == (6, 8)
    assert node_history == (6, 8)
    assert len(settings_reads) == 1


@pytest.mark.asyncio
async def test_a_reset_between_the_poll_and_the_write_keeps_outbound_history_and_node_bytes_aligned(
    monkeypatch: pytest.MonkeyPatch, session_factory
):
    node_id = await _seed_node(session_factory)
    async with session_factory() as session:
        node = await session.get(Node, node_id)
        node.uplink, node.downlink = 100, 200
        await session.commit()

    async def reset_then_return_the_sample():
        async with session_factory() as reset_session:
            db_node = await reset_session.get(Node, node_id)
            await reset_node_usage(reset_session, db_node)
        return [{"tag": "direct", "up": 7, "down": 9}]

    settings_reads: list = []
    _patch_job(
        monkeypatch, node_id, [reset_then_return_the_sample, [{"tag": "direct", "up": 1, "down": 1}]], settings_reads
    )

    await record_usages.record_node_usages()
    await record_usages.record_node_usages()

    node, outbound, node_history, logged = await _totals(session_factory, node_id)
    assert logged == (100, 200)
    assert node == (8, 10)
    assert outbound == {"direct": (8, 10)}
    assert node_history == (8, 10)
    assert len(settings_reads) == 2


async def _seed_two_nodes(session_factory) -> tuple[int, int]:
    async with session_factory() as session:
        doomed = Node(
            name="doomed",
            address="10.0.0.2",
            port=1002,
            api_port=1003,
            server_ca="ca2",
            api_key="key2",
            core_config_id=None,
        )
        kept = Node(
            name="kept",
            address="10.0.0.3",
            port=1004,
            api_port=1005,
            server_ca="ca3",
            api_key="key3",
            core_config_id=None,
        )
        session.add_all([doomed, kept, System(uplink=0, downlink=0)])
        await session.flush()
        ids = (doomed.id, kept.id)
        await session.commit()
    return ids


async def _delete_node(session_factory, node_id: int) -> None:
    async with session_factory() as session:
        await remove_node(session, await session.get(Node, node_id))


@pytest.mark.asyncio
@pytest.mark.parametrize("deleted_during", ["poll", "write"])
async def test_a_node_deleted_while_its_outbound_usage_is_recorded_leaves_no_orphan_rows_and_loses_no_live_bytes(
    monkeypatch: pytest.MonkeyPatch, session_factory, deleted_during
):
    doomed_id, kept_id = await _seed_two_nodes(session_factory)
    samples = {
        doomed_id: [{"tag": "direct", "up": 3, "down": 4}],
        kept_id: [{"tag": "direct", "up": 5, "down": 6}, {"tag": "warp", "up": 1, "down": 2}],
    }
    monkeypatch.setattr(record_usages, "_retained_node_usage", [])
    monkeypatch.setattr(
        record_usages.node_manager,
        "get_healthy_nodes",
        AsyncMock(return_value=[(doomed_id, DummyNode(doomed_id)), (kept_id, DummyNode(kept_id))]),
    )

    async def fake_get_outbounds_stats(node: DummyNode, node_id: int | None = None):
        if deleted_during == "poll" and node_id == doomed_id:
            await _delete_node(session_factory, doomed_id)
        return samples[node_id]

    async def outbound_settings():
        return General(usage=UsageSettings(record_node_usage=True, record_outbound_usage=True))

    monkeypatch.setattr(record_usages, "get_outbounds_stats", fake_get_outbounds_stats)
    monkeypatch.setattr(record_usages, "general_settings", outbound_settings)
    gate = asyncio.Semaphore(1)
    monkeypatch.setattr(record_usages, "JOB_SEM", gate)
    real_write = record_usages.write_node_usage
    write_entered = asyncio.Event()
    writes = []

    async def observed_write(node_totals, *args, **kwargs):
        writes.append(dict(node_totals))
        write_entered.set()
        return await real_write(node_totals, *args, **kwargs)

    monkeypatch.setattr(record_usages, "write_node_usage", observed_write)

    await gate.acquire()
    job = asyncio.create_task(record_usages.record_node_usages())
    try:
        await asyncio.wait_for(write_entered.wait(), timeout=5)
        await asyncio.sleep(0)
        assert not job.done()
        if deleted_during == "write":
            await _delete_node(session_factory, doomed_id)
    finally:
        gate.release()
    await asyncio.wait_for(job, timeout=10)

    assert writes == [{doomed_id: (3, 4), kept_id: (6, 8)}]
    assert record_usages._retained_node_usage == []
    async with session_factory() as session:
        assert await session.get(Node, doomed_id) is None
        rows = (
            await session.execute(
                select(
                    NodeOutboundUsage.node_id,
                    NodeOutboundUsage.outbound_tag,
                    NodeOutboundUsage.uplink,
                    NodeOutboundUsage.downlink,
                ).order_by(NodeOutboundUsage.node_id, NodeOutboundUsage.outbound_tag)
            )
        ).all()
        system = (await session.execute(select(System.uplink, System.downlink))).one()
        node_history = (
            await session.execute(
                select(func.coalesce(func.sum(NodeUsage.uplink), 0), func.coalesce(func.sum(NodeUsage.downlink), 0))
            )
        ).one()
    assert [tuple(row) for row in rows] == [(kept_id, "direct", 5, 6), (kept_id, "warp", 1, 2)]
    _, outbound, _, _ = await _totals(session_factory, kept_id)
    assert outbound == {"direct": (5, 6), "warp": (1, 2)}
    node, _, _, _ = await _totals(session_factory, kept_id)
    assert node == (6, 8)
    assert tuple(system) == (9, 12)
    assert tuple(node_history) == (9, 12)


class _PausingConnection:
    def __init__(self, conn, on_existence_check):
        self._conn = conn
        self._on_existence_check = on_existence_check

    def __getattr__(self, name):
        return getattr(self._conn, name)

    async def execute(self, statement, *args, **kwargs):
        result = await self._conn.execute(statement, *args, **kwargs)
        if str(statement).startswith("SELECT nodes.id"):
            await self._on_existence_check()
        return result


@pytest.mark.asyncio
async def test_a_node_deleted_inside_the_write_transaction_after_the_existence_check_leaves_no_orphan_rows(
    monkeypatch: pytest.MonkeyPatch, session_factory
):
    async with session_factory() as session:
        dialect_name = session.bind.dialect.name
    if dialect_name == "sqlite":
        pytest.skip("needs a real database with row locks and concurrent writers")
    lock_waits_sql = text(
        "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'"
        if dialect_name == "postgresql"
        else "SELECT count(*) FROM information_schema.innodb_trx WHERE trx_state = 'LOCK WAIT'"
    )
    doomed_id, kept_id = await _seed_two_nodes(session_factory)
    samples = {
        doomed_id: [{"tag": "direct", "up": 3, "down": 4}],
        kept_id: [{"tag": "direct", "up": 5, "down": 6}, {"tag": "warp", "up": 1, "down": 2}],
    }
    monkeypatch.setattr(record_usages, "_retained_node_usage", [])
    monkeypatch.setattr(
        record_usages.node_manager,
        "get_healthy_nodes",
        AsyncMock(return_value=[(doomed_id, DummyNode(doomed_id)), (kept_id, DummyNode(kept_id))]),
    )

    async def fake_get_outbounds_stats(node: DummyNode, node_id: int | None = None):
        return samples[node_id]

    async def outbound_settings():
        return General(usage=UsageSettings(record_node_usage=True, record_outbound_usage=True))

    monkeypatch.setattr(record_usages, "get_outbounds_stats", fake_get_outbounds_stats)
    monkeypatch.setattr(record_usages, "general_settings", outbound_settings)
    real_run = record_usages.run_in_retried_transaction
    deletes: list[asyncio.Task] = []
    attempts = {"n": 0}
    delete_blocked_inside_the_write = []

    async def delete_once_after_the_existence_check():
        if deletes:
            return
        deletes.append(asyncio.create_task(_delete_node(session_factory, doomed_id)))
        for _ in range(50):
            await asyncio.sleep(0.1)
            async with session_factory() as probe:
                if (await probe.execute(lock_waits_sql)).scalar_one() > 0:
                    delete_blocked_inside_the_write.append(not deletes[0].done())
                    return

    async def run_with_a_concurrent_delete(operation, *args, **kwargs):
        async def paused(conn):
            attempts["n"] += 1
            return await operation(_PausingConnection(conn, delete_once_after_the_existence_check))

        return await real_run(paused, *args, **kwargs)

    monkeypatch.setattr(record_usages, "run_in_retried_transaction", run_with_a_concurrent_delete)

    await asyncio.wait_for(record_usages.record_node_usages(), timeout=30)
    assert len(deletes) == 1
    assert delete_blocked_inside_the_write == [True]
    await asyncio.wait_for(deletes[0], timeout=30)

    assert record_usages._retained_node_usage == []
    async with session_factory() as session:
        assert await session.get(Node, doomed_id) is None
        orphan_rows = (
            await session.execute(
                select(func.count())
                .select_from(NodeOutboundUsage)
                .where(NodeOutboundUsage.node_id.not_in(select(Node.id)))
            )
        ).scalar_one()
        rows = (
            await session.execute(
                select(
                    NodeOutboundUsage.node_id,
                    NodeOutboundUsage.outbound_tag,
                    NodeOutboundUsage.uplink,
                    NodeOutboundUsage.downlink,
                ).order_by(NodeOutboundUsage.node_id, NodeOutboundUsage.outbound_tag)
            )
        ).all()
        system = (await session.execute(select(System.uplink, System.downlink))).one()
        node_history = (
            await session.execute(
                select(func.coalesce(func.sum(NodeUsage.uplink), 0), func.coalesce(func.sum(NodeUsage.downlink), 0))
            )
        ).one()
    assert orphan_rows == 0
    assert [tuple(row) for row in rows] == [(kept_id, "direct", 5, 6), (kept_id, "warp", 1, 2)]
    node, _, _, _ = await _totals(session_factory, kept_id)
    assert node == (6, 8)
    assert tuple(system) == (9, 12)
    assert tuple(node_history) == (9, 12)
    assert attempts["n"] >= 1
