from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app.db.crud.node import remove_node
from app.db.models import Node, NodeUsage, System
from app.jobs import record_usages
from tests.test_recorder_harness import (  # noqa: F401
    recorder_db_fixture,
    seed_users_and_nodes,
    statement_faults_fixture,
)


class _Deadlock(Exception):
    args = (1213, "Deadlock found when trying to get lock; try restarting transaction")


class _ConnectionLost(Exception):
    args = (2013, "Lost connection to MySQL server during query")


class OutboundNode:
    def __init__(self, pending: int):
        self.pending = pending

    async def get_stats(self, stat_type, reset: bool, timeout: int):
        value, self.pending = self.pending, 0
        return SimpleNamespace(stats=[SimpleNamespace(type="downlink", value=value)] if value else [])


def _starts_with(prefix: str):
    return lambda statement: " ".join(statement.upper().replace("`", "").replace('"', "").split()).startswith(prefix)


async def _install(monkeypatch: pytest.MonkeyPatch, recorder_db, nodes: dict[int, OutboundNode]) -> None:
    async with recorder_db() as session:
        session.add(System(uplink=0, downlink=0))
        await session.commit()
    monkeypatch.setattr(record_usages.node_manager, "get_healthy_nodes", AsyncMock(return_value=list(nodes.items())))
    monkeypatch.setattr(record_usages, "after_record_node_usages", AsyncMock())


async def _state(recorder_db, node_id: int) -> tuple[int, int, int]:
    async with recorder_db() as session:
        downlink = (await session.execute(select(Node.downlink).where(Node.id == node_id))).scalar_one()
        charted = (
            await session.execute(
                select(func.coalesce(func.sum(NodeUsage.downlink), 0)).where(NodeUsage.node_id == node_id)
            )
        ).scalar_one()
        system = (await session.execute(select(System.downlink))).scalar_one()
    return downlink, charted, system


def _retained() -> list[tuple[dict, int]]:
    return [(entry.totals, entry.attempts) for entry in record_usages._retained_node_usage]


@pytest.mark.asyncio
async def test_node_level_usage_is_recorded_consistently_or_kept_when_the_chart_write_fails(
    monkeypatch: pytest.MonkeyPatch, recorder_db, statement_faults
):
    _, _, node_ids = await seed_users_and_nodes(recorder_db, user_count=0, node_count=1)
    node_id = node_ids[0]
    await _install(monkeypatch, recorder_db, {node_id: OutboundNode(4_000)})
    failing = {"on": True}
    chart_insert = _starts_with("INSERT INTO NODE_USAGES")
    statement_faults(
        lambda statement: failing["on"] and chart_insert(statement),
        OperationalError("INSERT INTO node_usages", {}, _Deadlock()),
    )

    with pytest.raises(OperationalError):
        await record_usages._record_node_usages_impl()

    assert await _state(recorder_db, node_id) == (0, 0, 0)
    assert _retained() == [({node_id: (0, 4_000)}, 1)]

    failing["on"] = False
    await record_usages._record_node_usages_impl()

    assert await _state(recorder_db, node_id) == (4_000, 4_000, 4_000)
    assert _retained() == []


@pytest.mark.asyncio
async def test_a_failed_system_write_keeps_the_node_counters_unwritten_and_the_bytes_held(
    monkeypatch: pytest.MonkeyPatch, recorder_db, statement_faults
):
    _, _, node_ids = await seed_users_and_nodes(recorder_db, user_count=0, node_count=1)
    node_id = node_ids[0]
    node = OutboundNode(2_500)
    await _install(monkeypatch, recorder_db, {node_id: node})
    statement_faults(_starts_with("UPDATE SYSTEM"), OperationalError("UPDATE system", {}, _Deadlock()), {1, 2, 3, 4, 5})

    with pytest.raises(OperationalError):
        await record_usages._record_node_usages_impl()

    assert await _state(recorder_db, node_id) == (0, 0, 0)

    node.pending = 500
    await record_usages._record_node_usages_impl()

    assert await _state(recorder_db, node_id) == (3_000, 3_000, 3_000)
    assert _retained() == []


@pytest.mark.asyncio
async def test_node_usage_that_keeps_failing_is_abandoned_at_the_retry_bound(
    monkeypatch: pytest.MonkeyPatch, recorder_db, statement_faults, caplog
):
    _, _, node_ids = await seed_users_and_nodes(recorder_db, user_count=0, node_count=1)
    node_id = node_ids[0]
    node = OutboundNode(700)
    await _install(monkeypatch, recorder_db, {node_id: node})
    monkeypatch.setattr(record_usages, "USAGE_RETAINED_MAX_ATTEMPTS", 2)
    statement_faults(_starts_with("UPDATE NODES"), OperationalError("UPDATE nodes", {}, _Deadlock()))
    caplog.set_level(logging.WARNING, logger="record-usages")

    for _ in range(2):
        with pytest.raises(OperationalError):
            await record_usages._record_node_usages_impl()

    assert _retained() == []
    assert any("Gave up on 700 node usage bytes" in record.getMessage() for record in caplog.records)
    assert await _state(recorder_db, node_id) == (0, 0, 0)


@pytest.mark.asyncio
async def test_node_usage_whose_commit_is_in_doubt_is_not_written_twice(
    monkeypatch: pytest.MonkeyPatch, recorder_db, statement_faults, caplog
):
    _, _, node_ids = await seed_users_and_nodes(recorder_db, user_count=0, node_count=1)
    node_id = node_ids[0]
    await _install(monkeypatch, recorder_db, {node_id: OutboundNode(900)})
    statement_faults(
        _starts_with("INSERT INTO NODE_USAGES"),
        OperationalError("INSERT INTO node_usages", {}, _ConnectionLost(), connection_invalidated=True),
        {1},
    )
    caplog.set_level(logging.WARNING, logger="record-usages")

    with pytest.raises(OperationalError):
        await record_usages._record_node_usages_impl()

    assert _retained() == []
    assert any("may have committed" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_node_usage_of_a_node_deleted_mid_tick_is_charted_without_a_node_and_the_rest_is_written(
    monkeypatch: pytest.MonkeyPatch, recorder_db
):
    _, _, node_ids = await seed_users_and_nodes(recorder_db, user_count=0, node_count=2)
    kept_id, doomed_id = node_ids
    await _install(monkeypatch, recorder_db, {kept_id: OutboundNode(1_000), doomed_id: OutboundNode(300)})
    real_gather = record_usages.asyncio.gather
    deleted = {"done": False}

    async def gather_then_delete(*aws, **kwargs):
        results = await real_gather(*aws, **kwargs)
        if not deleted["done"]:
            deleted["done"] = True
            async with recorder_db() as session:
                await remove_node(session, await session.get(Node, doomed_id))
        return results

    monkeypatch.setattr(record_usages.asyncio, "gather", gather_then_delete)

    await record_usages._record_node_usages_impl()

    assert deleted["done"]
    async with recorder_db() as session:
        charted = dict(
            (
                await session.execute(
                    select(NodeUsage.node_id, func.sum(NodeUsage.downlink)).group_by(NodeUsage.node_id)
                )
            ).all()
        )
        kept_downlink = (await session.execute(select(Node.downlink).where(Node.id == kept_id))).scalar_one()
        system = (await session.execute(select(System.downlink))).scalar_one()
    assert kept_downlink == 1_000
    assert charted == {kept_id: 1_000, None: 300}
    assert system == 1_300 == sum(charted.values())
    assert _retained() == []
