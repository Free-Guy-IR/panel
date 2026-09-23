from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from PasarGuardNodeBridge import NodeAPIError
from PasarGuardNodeBridge.common.service_pb2 import StatType
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app.db.crud.node import remove_node
from app.db.models import Node, NodeInboundUsage, NodeUsage, System
from app.fork.jobs import inbound_usage, stat_batches
from app.fork.models.node_stat_ack import NodeStatAck
from app.jobs import record_usages
from tests.test_recorder_harness import (  # noqa: F401
    CountingNode,
    is_billing_write,
    recorder_db_fixture,
    seed_users_and_nodes,
    statement_faults_fixture,
    usage_snapshot,
)


class _ConnectionGone(Exception):
    args = (2003, "Can't connect to MySQL server")


class TwoPhaseNode:
    def __init__(self, counters: dict, epoch: str | None = None, speaks_two_phase: bool = True, delay: float = 0.0):
        self.delay = delay
        self.counters = dict(counters)
        self.epoch = epoch or uuid4().hex
        self.speaks_two_phase = speaks_two_phase
        self.seq = 0
        self.pending: dict = {}
        self.requests: list[tuple] = []
        self.lose_next_response = False
        self.lose_next_request = False

    async def get_extra(self) -> dict:
        return {"usage_coefficient": 1.0}

    def add(self, key, value: int) -> None:
        self.counters[key] = self.counters.get(key, 0) + value

    def restart(self) -> None:
        self.epoch = uuid4().hex
        self.seq = 0
        self.pending = {}

    def _drain(self) -> dict:
        drained = {key: value for key, value in self.counters.items() if value}
        self.counters = {key: 0 for key in self.counters}
        return drained

    def _stats(self, stat_type, values: dict) -> list:
        if stat_type == StatType.Outbounds:
            return [SimpleNamespace(name="direct", type="downlink", value=value) for value in values.values()]
        if stat_type == StatType.Inbounds:
            return [
                SimpleNamespace(name=str(key), type="downlink", link="", value=value) for key, value in values.items()
            ]
        return [SimpleNamespace(name=str(key), value=value) for key, value in values.items()]

    async def get_stats(
        self, stat_type, reset=True, name="", timeout=None, *, hold=False, ack_epoch="", ack_batch_id=0
    ):
        if self.lose_next_request:
            self.lose_next_request = False
            raise NodeAPIError(-1, "Stream terminated: Connection lost")
        self.requests.append((StatType.Name(stat_type), hold, ack_epoch, ack_batch_id))
        if self.speaks_two_phase and hold and reset and not name:
            held = self.pending.get(stat_type)
            if held is not None and ack_epoch == self.epoch and ack_batch_id >= held[0]:
                held = None
                self.pending.pop(stat_type)
            if held is None:
                drained = self._drain()
                if drained:
                    self.seq += 1
                    held = (self.seq, drained)
                    self.pending[stat_type] = held
            batch_id, values = held or (0, {})
            response = SimpleNamespace(stats=self._stats(stat_type, values), batch_epoch=self.epoch, batch_id=batch_id)
        else:
            response = SimpleNamespace(stats=self._stats(stat_type, self._drain() if reset else dict(self.counters)))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.lose_next_response:
            self.lose_next_response = False
            raise NodeAPIError(-1, "Request timed out")
        return response


@pytest.fixture(autouse=True)
def _fresh_batch_memory(monkeypatch):
    monkeypatch.setattr(stat_batches, "_epoch_by_node", {})
    monkeypatch.setattr(stat_batches, "_two_phase_by_class", {})


def _install(monkeypatch, pairs):
    monkeypatch.setattr(record_usages.node_manager, "get_healthy_nodes", AsyncMock(return_value=list(pairs)))


async def _acks(recorder_db) -> list[tuple]:
    async with recorder_db() as session:
        rows = (
            await session.execute(
                select(NodeStatAck.node_id, NodeStatAck.stat_type, NodeStatAck.batch_epoch, NodeStatAck.batch_id)
            )
        ).all()
    return sorted(tuple(row) for row in rows)


@pytest.mark.asyncio
async def test_bytes_drained_by_a_reset_read_whose_response_is_lost_are_still_billed(monkeypatch, recorder_db):
    _, user_ids, node_ids = await seed_users_and_nodes(recorder_db, user_count=1, node_count=1)
    uid, node_id = user_ids[0], node_ids[0]
    node = TwoPhaseNode({uid: 0})
    _install(monkeypatch, [(node_id, node)])

    node.add(uid, 5_000)
    node.lose_next_response = True
    await record_usages._record_user_usages_impl()
    assert record_usages._retained_cohorts == []

    node.add(uid, 700)
    await record_usages._record_user_usages_impl()
    billed, charted = await usage_snapshot(recorder_db, user_ids)
    assert billed == {uid: 5_000}

    await record_usages._record_user_usages_impl()

    billed, charted = await usage_snapshot(recorder_db, user_ids)
    assert billed == {uid: 5_700}
    assert charted == billed
    assert await _acks(recorder_db) == [(node_id, StatType.UsersStat, node.epoch, 2)]
    assert [request[3] for request in node.requests] == [0, 0, 1]


@pytest.mark.asyncio
async def test_an_old_node_that_loses_a_drained_response_behaves_exactly_as_before(monkeypatch, recorder_db):
    _, user_ids, node_ids = await seed_users_and_nodes(recorder_db, user_count=1, node_count=1)
    uid, node_id = user_ids[0], node_ids[0]
    node = TwoPhaseNode({uid: 0}, speaks_two_phase=False)
    _install(monkeypatch, [(node_id, node)])

    node.add(uid, 5_000)
    node.lose_next_response = True
    await record_usages._record_user_usages_impl()
    node.add(uid, 700)
    await record_usages._record_user_usages_impl()

    billed, charted = await usage_snapshot(recorder_db, user_ids)
    assert billed == {uid: 700}
    assert charted == billed
    assert await _acks(recorder_db) == []
    assert all(request[1] for request in node.requests)


@pytest.mark.asyncio
async def test_an_old_bridge_is_called_exactly_as_before(monkeypatch, recorder_db):
    _, user_ids, node_ids = await seed_users_and_nodes(recorder_db, user_count=1, node_count=1)
    node = CountingNode(node_ids[0], {user_ids[0]: 900})
    _install(monkeypatch, [(node.node_id, node)])

    await record_usages._record_user_usages_impl()

    billed, _ = await usage_snapshot(recorder_db, user_ids)
    assert billed == {user_ids[0]: 900}
    assert not stat_batches.supports_two_phase(node)
    assert await _acks(recorder_db) == []


@pytest.mark.asyncio
async def test_a_failed_billing_write_leaves_the_batch_on_the_node_and_bills_it_once(
    monkeypatch, recorder_db, statement_faults
):
    _, user_ids, node_ids = await seed_users_and_nodes(recorder_db, user_count=2, node_count=1)
    node = TwoPhaseNode({user_ids[0]: 300, user_ids[1]: 70})
    _install(monkeypatch, [(node_ids[0], node)])
    statement_faults(is_billing_write, OperationalError("UPDATE users", {}, _ConnectionGone()), {1})

    with pytest.raises(OperationalError):
        await record_usages._record_user_usages_impl()
    assert record_usages._retained_cohorts == []
    billed, _ = await usage_snapshot(recorder_db, user_ids)
    assert billed == {user_ids[0]: 0, user_ids[1]: 0}

    node.add(user_ids[0], 5)
    await record_usages._record_user_usages_impl()
    await record_usages._record_user_usages_impl()

    billed, charted = await usage_snapshot(recorder_db, user_ids)
    assert billed == {user_ids[0]: 305, user_ids[1]: 70}
    assert charted == billed


@pytest.mark.asyncio
async def test_a_commit_whose_outcome_was_in_doubt_is_neither_lost_nor_billed_twice(monkeypatch, recorder_db):
    _, user_ids, node_ids = await seed_users_and_nodes(recorder_db, user_count=1, node_count=1)
    uid = user_ids[0]
    node = TwoPhaseNode({uid: 1_234})
    _install(monkeypatch, [(node_ids[0], node)])
    real = record_usages.run_in_retried_transaction
    raised = {"done": False}

    async def commits_then_reports_a_dropped_connection(operation, max_retries=record_usages.DEADLOCK_MAX_RETRIES):
        result = await real(operation, max_retries)
        if not raised["done"]:
            raised["done"] = True
            raise OperationalError("COMMIT", {}, _ConnectionGone(), connection_invalidated=True)
        return result

    monkeypatch.setattr(record_usages, "run_in_retried_transaction", commits_then_reports_a_dropped_connection)

    with pytest.raises(OperationalError):
        await record_usages._record_user_usages_impl()
    await record_usages._record_user_usages_impl()
    node.add(uid, 6)
    await record_usages._record_user_usages_impl()

    billed, charted = await usage_snapshot(recorder_db, user_ids)
    assert billed == {uid: 1_240}
    assert charted == billed


@pytest.mark.asyncio
async def test_an_acknowledgement_that_never_reaches_the_node_is_sent_again(monkeypatch, recorder_db):
    _, user_ids, node_ids = await seed_users_and_nodes(recorder_db, user_count=1, node_count=1)
    uid = user_ids[0]
    node = TwoPhaseNode({uid: 400})
    _install(monkeypatch, [(node_ids[0], node)])

    await record_usages._record_user_usages_impl()
    node.add(uid, 50)
    node.lose_next_request = True
    await record_usages._record_user_usages_impl()
    await record_usages._record_user_usages_impl()

    billed, charted = await usage_snapshot(recorder_db, user_ids)
    assert billed == {uid: 450}
    assert charted == billed


@pytest.mark.asyncio
async def test_one_node_process_registered_twice_is_billed_once(monkeypatch, recorder_db):
    _, user_ids, node_ids = await seed_users_and_nodes(recorder_db, user_count=1, node_count=2)
    uid = user_ids[0]
    node = TwoPhaseNode({uid: 800})
    _install(monkeypatch, [(node_ids[0], node), (node_ids[1], node)])

    await record_usages._record_user_usages_impl()
    await record_usages._record_user_usages_impl()

    billed, charted = await usage_snapshot(recorder_db, user_ids)
    assert billed == {uid: 800}
    assert charted == billed


@pytest.mark.asyncio
async def test_a_node_deleted_and_added_again_does_not_bill_its_last_batch_twice(monkeypatch, recorder_db):
    _, user_ids, node_ids = await seed_users_and_nodes(recorder_db, user_count=1, node_count=2)
    uid = user_ids[0]
    first_id, second_id = node_ids
    node = TwoPhaseNode({uid: 1_000})
    _install(monkeypatch, [(first_id, node)])
    await record_usages._record_user_usages_impl()

    async with recorder_db() as session:
        await remove_node(session, await session.get(Node, first_id))
    monkeypatch.setattr(stat_batches, "_epoch_by_node", {})
    _install(monkeypatch, [(second_id, node)])
    node.add(uid, 25)

    await record_usages._record_user_usages_impl()
    await record_usages._record_user_usages_impl()

    billed, charted = await usage_snapshot(recorder_db, user_ids)
    assert billed == {uid: 1_025}
    assert charted == billed
    assert await _acks(recorder_db) == [(second_id, StatType.UsersStat, node.epoch, 2)]


@pytest.mark.asyncio
async def test_a_restarted_node_starts_a_new_epoch_and_its_old_acknowledgement_is_dropped(monkeypatch, recorder_db):
    _, user_ids, node_ids = await seed_users_and_nodes(recorder_db, user_count=1, node_count=1)
    uid, node_id = user_ids[0], node_ids[0]
    node = TwoPhaseNode({uid: 100})
    _install(monkeypatch, [(node_id, node)])
    await record_usages._record_user_usages_impl()
    old_epoch = node.epoch

    node.restart()
    node.add(uid, 30)
    await record_usages._record_user_usages_impl()

    billed, _ = await usage_snapshot(recorder_db, user_ids)
    assert billed == {uid: 130}
    assert node.requests[-1][2] == old_epoch
    assert await _acks(recorder_db) == [(node_id, StatType.UsersStat, node.epoch, 1)]


@pytest.mark.asyncio
async def test_bytes_nobody_is_billed_for_are_still_acknowledged(monkeypatch, recorder_db):
    _, _, node_ids = await seed_users_and_nodes(recorder_db, user_count=1, node_count=1)
    node = TwoPhaseNode({987_654: 500})
    _install(monkeypatch, [(node_ids[0], node)])

    await record_usages._record_user_usages_impl()
    await record_usages._record_user_usages_impl()

    assert await _acks(recorder_db) == [(node_ids[0], StatType.UsersStat, node.epoch, 1)]
    assert node.pending == {}


@pytest.mark.asyncio
async def test_a_collection_stopped_mid_tick_keeps_two_phase_bytes_on_the_node_not_in_memory(
    monkeypatch, recorder_db, statement_faults
):
    _, user_ids, node_ids = await seed_users_and_nodes(recorder_db, user_count=2, node_count=2)
    fast = TwoPhaseNode({user_ids[0]: 640})
    slow = TwoPhaseNode({user_ids[1]: 90}, delay=0.3)
    _install(monkeypatch, [(node_ids[0], fast), (node_ids[1], slow)])
    monkeypatch.setattr(record_usages, "USAGE_PERSIST_COHORT_SIZE", 1)
    monkeypatch.setattr(record_usages, "USAGE_PERSIST_MAX_VOLATILE_S", 0.0)
    faults = statement_faults(is_billing_write, OperationalError("UPDATE users", {}, _ConnectionGone()), {1})

    with pytest.raises(OperationalError):
        await record_usages._record_user_usages_impl()

    assert faults.matched == 1
    assert slow.pending and fast.pending
    assert record_usages._retained_cohorts == []

    await record_usages._record_user_usages_impl()
    await record_usages._record_user_usages_impl()

    billed, charted = await usage_snapshot(recorder_db, user_ids)
    assert billed == {user_ids[0]: 640, user_ids[1]: 90}
    assert charted == billed


async def _outbound_state(recorder_db, node_id: int) -> tuple[int, int, int]:
    async with recorder_db() as session:
        downlink = (await session.execute(select(Node.downlink).where(Node.id == node_id))).scalar_one()
        charted = (
            await session.execute(
                select(func.coalesce(func.sum(NodeUsage.downlink), 0)).where(NodeUsage.node_id == node_id)
            )
        ).scalar_one()
        system = (await session.execute(select(System.downlink))).scalar_one()
    return downlink, charted, system


@pytest.mark.asyncio
async def test_outbound_bytes_whose_response_is_lost_are_recorded_once(monkeypatch, recorder_db):
    _, _, node_ids = await seed_users_and_nodes(recorder_db, user_count=0, node_count=1)
    node_id = node_ids[0]
    async with recorder_db() as session:
        session.add(System(uplink=0, downlink=0))
        await session.commit()
    node = TwoPhaseNode({"direct": 3_000})
    _install(monkeypatch, [(node_id, node)])
    monkeypatch.setattr(record_usages, "after_record_node_usages", AsyncMock())

    node.lose_next_response = True
    await record_usages._record_node_usages_impl()
    assert await _outbound_state(recorder_db, node_id) == (0, 0, 0)
    assert record_usages._retained_node_usage == []

    node.add("direct", 11)
    await record_usages._record_node_usages_impl()
    await record_usages._record_node_usages_impl()

    assert await _outbound_state(recorder_db, node_id) == (3_011, 3_011, 3_011)
    assert await _acks(recorder_db) == [(node_id, StatType.Outbounds, node.epoch, 2)]


@pytest.mark.asyncio
async def test_outbound_bytes_of_one_node_process_registered_twice_are_recorded_once(monkeypatch, recorder_db):
    _, _, node_ids = await seed_users_and_nodes(recorder_db, user_count=0, node_count=2)
    async with recorder_db() as session:
        session.add(System(uplink=0, downlink=0))
        await session.commit()
    node = TwoPhaseNode({"direct": 900})
    _install(monkeypatch, [(node_ids[0], node), (node_ids[1], node)])
    monkeypatch.setattr(record_usages, "after_record_node_usages", AsyncMock())

    await record_usages._record_node_usages_impl()
    await record_usages._record_node_usages_impl()

    first = await _outbound_state(recorder_db, node_ids[0])
    second = await _outbound_state(recorder_db, node_ids[1])
    assert first[0] + second[0] == 900
    assert first[2] == 900


@pytest.mark.asyncio
async def test_inbound_bytes_whose_response_is_lost_are_recorded_once(monkeypatch, recorder_db):
    _, _, node_ids = await seed_users_and_nodes(recorder_db, user_count=0, node_count=1)
    node_id = node_ids[0]
    node = TwoPhaseNode({"vless-in": 2_000})
    monkeypatch.setattr(inbound_usage, "GetDB", record_usages.GetDB)
    bucket = record_usages._get_time_bucket()

    node.lose_next_response = True
    await inbound_usage.record_inbound_usages([(node_id, node)], bucket)
    node.add("vless-in", 7)
    await inbound_usage.record_inbound_usages([(node_id, node)], bucket)
    await inbound_usage.record_inbound_usages([(node_id, node)], bucket)

    async with recorder_db() as session:
        recorded = (
            await session.execute(
                select(func.sum(NodeInboundUsage.downlink)).where(NodeInboundUsage.node_id == node_id)
            )
        ).scalar_one()
    assert recorded == 2_007
    assert await _acks(recorder_db) == [(node_id, StatType.Inbounds, node.epoch, 2)]


@pytest.mark.asyncio
async def test_a_two_phase_capable_bridge_talking_to_an_old_node_keeps_the_legacy_retention(
    monkeypatch, recorder_db, statement_faults
):
    _, user_ids, node_ids = await seed_users_and_nodes(recorder_db, user_count=1, node_count=1)
    uid = user_ids[0]
    node = TwoPhaseNode({uid: 210}, speaks_two_phase=False)
    _install(monkeypatch, [(node_ids[0], node)])
    statement_faults(is_billing_write, OperationalError("UPDATE users", {}, _ConnectionGone()), {1})

    with pytest.raises(OperationalError):
        await record_usages._record_user_usages_impl()
    assert [record_usages._raw_bytes(cohort.api_params) for cohort in record_usages._retained_cohorts] == [210]

    await record_usages._record_user_usages_impl()
    billed, charted = await usage_snapshot(recorder_db, user_ids)
    assert billed == {uid: 210}
    assert charted == billed
