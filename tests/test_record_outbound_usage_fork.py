from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app.db.crud.node import reset_node_usage
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
