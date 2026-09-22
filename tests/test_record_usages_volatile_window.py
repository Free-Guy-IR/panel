from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import OperationalError

from app.jobs import record_usages


class _ConnectionGone(Exception):
    args = (2003, "Can't connect to MySQL server")


class _LockWaitTimeout(Exception):
    args = (1205, "Lock wait timeout exceeded")


class _StubNode:
    def __init__(self, node_id: int, usage_coefficient: float = 1.0):
        self.node_id = node_id
        self.usage_coefficient = usage_coefficient

    async def get_extra(self) -> dict:
        return {"id": self.node_id, "usage_coefficient": self.usage_coefficient}


class _FrozenClock:
    def __init__(self, value: float = 1000.0):
        self.value = value

    def monotonic(self) -> float:
        return self.value

    def time(self) -> float:
        return self.value


@pytest.fixture(autouse=True)
def _reset_module_state():
    record_usages._usage_coefficient_cache.clear()
    record_usages._user_usage_running = False
    record_usages._node_usage_running = False
    record_usages._unknown_uid_last_log_at = None
    record_usages._unknown_uid_suppressed_reports = 0
    yield
    record_usages._usage_coefficient_cache.clear()
    record_usages._user_usage_running = False
    record_usages._node_usage_running = False
    record_usages._unknown_uid_last_log_at = None
    record_usages._unknown_uid_suppressed_reports = 0


@pytest.mark.asyncio
async def test_fast_node_usage_is_persisted_before_the_slow_node_returns(monkeypatch: pytest.MonkeyPatch):
    fast_node_id, slow_node_id = 11, 22
    fast_user_id, slow_user_id = 101, 202
    slow_release = asyncio.Event()
    state = {"slow_finished": False}

    async def fake_get_users_stats(node, node_id=None):
        if node.node_id == slow_node_id:
            try:
                await asyncio.wait_for(slow_release.wait(), timeout=3.0)
            except TimeoutError:
                pass
            state["slow_finished"] = True
            return [{"uid": slow_user_id, "value": 70}]
        return [{"uid": fast_user_id, "value": 50}]

    persisted_batches: list[tuple[list[int], bool]] = []

    async def fake_record_user_stats_batched(all_node_params, usage_coefficients):
        persisted_batches.append((sorted(all_node_params), state["slow_finished"]))
        slow_release.set()

    context = {
        fast_user_id: record_usages.UserUsageContext(admin_id=None, usage_epoch=0),
        slow_user_id: record_usages.UserUsageContext(admin_id=None, usage_epoch=0),
    }

    monkeypatch.setattr(
        record_usages.node_manager,
        "get_healthy_nodes",
        AsyncMock(return_value=[(fast_node_id, _StubNode(fast_node_id)), (slow_node_id, _StubNode(slow_node_id))]),
    )
    monkeypatch.setattr(record_usages, "get_users_stats", fake_get_users_stats)
    monkeypatch.setattr(record_usages, "current_usage_epochs", AsyncMock(return_value={uid: 0 for uid in context}))
    monkeypatch.setattr(record_usages, "load_user_usage_context", AsyncMock(return_value=context))
    monkeypatch.setattr(record_usages, "record_user_stats_batched", fake_record_user_stats_batched)
    monkeypatch.setattr(
        record_usages,
        "apply_fenced_user_usage",
        AsyncMock(return_value=record_usages.FencedUsageOutcome(0, 0, [], 0)),
    )
    monkeypatch.setattr(record_usages.usage_settings, "disable_recording_node_usage", False)
    monkeypatch.setattr(record_usages, "USAGE_PERSIST_MAX_VOLATILE_S", 0.05)

    await record_usages._record_user_usages_impl()

    assert persisted_batches == [([fast_node_id], False), ([slow_node_id], True)]


@pytest.mark.asyncio
async def test_drain_still_yields_one_batch_when_every_node_is_fast():
    async def immediate(value):
        return value

    tasks = [asyncio.ensure_future(immediate(index)) for index in range(3)]
    batches = [batch async for batch in record_usages._drain_node_collection(tasks, 8, 5.0)]

    assert len(batches) == 1
    assert sorted(task.result() for task in batches[0]) == [0, 1, 2]


@pytest.mark.asyncio
async def test_drain_flushes_a_full_cohort_without_waiting_for_the_rest():
    blocker = asyncio.Event()

    async def immediate(value):
        return value

    async def blocked(value):
        await blocker.wait()
        return value

    tasks = [asyncio.ensure_future(immediate(index)) for index in range(3)]
    tasks.append(asyncio.ensure_future(blocked(99)))

    batches = []
    async for batch in record_usages._drain_node_collection(tasks, 3, 5.0):
        batches.append(sorted(task.result() for task in batch))
        blocker.set()

    assert batches == [[0, 1, 2], [99]]


@pytest.mark.asyncio
async def test_usage_job_warns_when_a_scheduler_cycle_was_skipped(monkeypatch: pytest.MonkeyPatch, caplog):
    record_usages._usage_job_last_start.clear()
    clock = _FrozenClock()
    monkeypatch.setattr(record_usages, "time", clock)

    async def impl():
        return None

    await record_usages._await_usage_job("record_user_usages", impl, 30, "JOB_RECORD_USER_USAGES_INTERVAL")

    clock.value += 75.0
    caplog.set_level(logging.WARNING, logger="record-usages")
    await record_usages._await_usage_job("record_user_usages", impl, 30, "JOB_RECORD_USER_USAGES_INTERVAL")

    assert "record_user_usages started 45.0s late" in caplog.text
    assert "75.0s since the previous run against a 30s interval" in caplog.text


@pytest.mark.asyncio
async def test_usage_job_stays_quiet_on_a_punctual_cycle(monkeypatch: pytest.MonkeyPatch, caplog):
    record_usages._usage_job_last_start.clear()
    clock = _FrozenClock()
    monkeypatch.setattr(record_usages, "time", clock)

    async def impl():
        return None

    await record_usages._await_usage_job("record_user_usages", impl, 30, "JOB_RECORD_USER_USAGES_INTERVAL")

    clock.value += 31.0
    caplog.set_level(logging.WARNING, logger="record-usages")
    await record_usages._await_usage_job("record_user_usages", impl, 30, "JOB_RECORD_USER_USAGES_INTERVAL")

    assert "late" not in caplog.text


def test_scheduler_never_runs_a_job_concurrently_with_itself():
    from app.scheduler import scheduler

    defaults = scheduler._job_defaults

    assert defaults["coalesce"] is True
    assert defaults["max_instances"] == 1


@pytest.mark.asyncio
async def test_unknown_uid_usage_is_counted_and_logged_once(caplog):
    caplog.set_level(logging.WARNING, logger="record-usages")
    before_count = record_usages._unknown_uid_total_count
    before_bytes = record_usages._unknown_uid_total_bytes

    dropped_users, dropped_bytes = record_usages._report_unknown_uid_usage(
        [{"uid": 1, "value": 100}, {"uid": 999, "value": 250}, {"uid": 1000, "value": 25}],
        {1},
    )

    assert (dropped_users, dropped_bytes) == (2, 275)
    assert record_usages._unknown_uid_total_count == before_count + 2
    assert record_usages._unknown_uid_total_bytes == before_bytes + 275

    warnings = [record for record in caplog.records if record.name == "record-usages"]
    assert len(warnings) == 1
    assert "275 bytes" in caplog.text
    assert "[999, 1000]" in caplog.text


def test_unknown_uid_log_sample_is_bounded(caplog):
    caplog.set_level(logging.WARNING, logger="record-usages")

    record_usages._report_unknown_uid_usage([{"uid": uid, "value": 1} for uid in range(500, 560)], set())

    warnings = [record for record in caplog.records if record.name == "record-usages"]
    assert len(warnings) == 1
    sample = warnings[0].args[3]
    assert len(sample) == record_usages.UNKNOWN_UID_LOG_SAMPLE


@pytest.mark.asyncio
async def test_orphan_uid_traffic_is_reported_by_the_usage_job(monkeypatch: pytest.MonkeyPatch, caplog):
    node_id = 31
    known_user_id, deleted_user_id = 7, 8

    async def fake_get_users_stats(node, node_id=None):
        return [{"uid": known_user_id, "value": 10}, {"uid": deleted_user_id, "value": 4096}]

    context = {known_user_id: record_usages.UserUsageContext(admin_id=None, usage_epoch=0)}

    monkeypatch.setattr(
        record_usages.node_manager, "get_healthy_nodes", AsyncMock(return_value=[(node_id, _StubNode(node_id))])
    )
    monkeypatch.setattr(record_usages, "get_users_stats", fake_get_users_stats)
    monkeypatch.setattr(record_usages, "current_usage_epochs", AsyncMock(return_value={known_user_id: 0}))
    monkeypatch.setattr(record_usages, "load_user_usage_context", AsyncMock(return_value=context))
    monkeypatch.setattr(record_usages, "record_user_stats_batched", AsyncMock())
    monkeypatch.setattr(
        record_usages,
        "apply_fenced_user_usage",
        AsyncMock(return_value=record_usages.FencedUsageOutcome(1, 0, [], 0)),
    )
    monkeypatch.setattr(record_usages.usage_settings, "disable_recording_node_usage", False)

    caplog.set_level(logging.WARNING, logger="record-usages")
    await record_usages._record_user_usages_impl()

    assert "4096 bytes of usage for 1 distinct uid(s) with no user row" in caplog.text
    assert f"on node(s) [{node_id}]" in caplog.text
    assert "[8]" in caplog.text


@pytest.mark.asyncio
async def test_repeat_orphan_reports_are_rate_limited_but_still_counted(caplog):
    caplog.set_level(logging.WARNING, logger="record-usages")
    before_bytes = record_usages._unknown_uid_total_bytes

    first = record_usages._report_unknown_uid_usage([{"uid": 4001, "value": 10}], set(), {7: [{"uid": 4001}]})
    second = record_usages._report_unknown_uid_usage([{"uid": 4002, "value": 25}], set(), {7: [{"uid": 4002}]})

    assert first == (1, 10)
    assert second == (1, 25)
    assert record_usages._unknown_uid_total_bytes == before_bytes + 35
    assert record_usages._unknown_uid_suppressed_reports == 1
    assert len([record for record in caplog.records if record.name == "record-usages"]) == 1


@pytest.mark.asyncio
async def test_a_failed_cohort_does_not_discard_the_other_cohorts(monkeypatch: pytest.MonkeyPatch):
    failing_node_id, healthy_node_id = 51, 52
    failing_user_id, healthy_user_id = 301, 302
    release = asyncio.Event()
    persisted_nodes: list[int] = []

    async def fake_get_users_stats(node, node_id=None):
        if node.node_id == healthy_node_id:
            try:
                await asyncio.wait_for(release.wait(), timeout=3.0)
            except TimeoutError:
                pass
            return [{"uid": healthy_user_id, "value": 90}]
        return [{"uid": failing_user_id, "value": 30}]

    async def fake_record_user_stats_batched(all_node_params, usage_coefficients):
        release.set()
        if failing_node_id in all_node_params:
            raise OperationalError("stmt", {}, _LockWaitTimeout())
        persisted_nodes.extend(sorted(all_node_params))

    context = {
        failing_user_id: record_usages.UserUsageContext(admin_id=None, usage_epoch=0),
        healthy_user_id: record_usages.UserUsageContext(admin_id=None, usage_epoch=0),
    }

    monkeypatch.setattr(
        record_usages.node_manager,
        "get_healthy_nodes",
        AsyncMock(
            return_value=[
                (failing_node_id, _StubNode(failing_node_id)),
                (healthy_node_id, _StubNode(healthy_node_id)),
            ]
        ),
    )
    monkeypatch.setattr(record_usages, "get_users_stats", fake_get_users_stats)
    monkeypatch.setattr(record_usages, "current_usage_epochs", AsyncMock(return_value={uid: 0 for uid in context}))
    monkeypatch.setattr(record_usages, "load_user_usage_context", AsyncMock(return_value=context))
    monkeypatch.setattr(record_usages, "record_user_stats_batched", fake_record_user_stats_batched)
    monkeypatch.setattr(
        record_usages,
        "apply_fenced_user_usage",
        AsyncMock(return_value=record_usages.FencedUsageOutcome(0, 0, [], 0)),
    )
    monkeypatch.setattr(record_usages.usage_settings, "disable_recording_node_usage", False)
    monkeypatch.setattr(record_usages, "USAGE_PERSIST_MAX_VOLATILE_S", 0.05)

    with pytest.raises(OperationalError):
        await record_usages._record_user_usages_impl()

    assert persisted_nodes == [healthy_node_id]


@pytest.mark.asyncio
async def test_volatile_budget_counts_from_task_completion_not_observation():
    blocker = asyncio.Event()

    async def immediate(value):
        return value

    async def after(delay, value):
        await asyncio.sleep(delay)
        return value

    async def blocked(value):
        await blocker.wait()
        return value

    tasks = [
        asyncio.ensure_future(immediate(1)),
        asyncio.ensure_future(after(0.3, 2)),
        asyncio.ensure_future(blocked(3)),
    ]

    loop = asyncio.get_running_loop()
    batches = []
    resumed_at = None
    second_batch_at = None

    async for batch in record_usages._drain_node_collection(tasks, 8, 0.2):
        values = sorted(task.result() for task in batch)
        batches.append(values)
        if values == [1]:
            await asyncio.sleep(0.4)
            resumed_at = loop.time()
        elif second_batch_at is None:
            second_batch_at = loop.time()
            blocker.set()

    assert batches == [[1], [2], [3]]
    assert second_batch_at - resumed_at < 0.1


@pytest.mark.asyncio
async def test_systemic_persistence_failure_stops_polling_the_remaining_nodes(monkeypatch: pytest.MonkeyPatch):
    first_node_id, second_node_id = 61, 62
    first_user_id, second_user_id = 401, 402
    polled: list[int] = []
    release = asyncio.Event()

    async def fake_get_users_stats(node, node_id=None):
        if node.node_id == second_node_id:
            try:
                await asyncio.wait_for(release.wait(), timeout=3.0)
            except TimeoutError:
                pass
            polled.append(second_node_id)
            return [{"uid": second_user_id, "value": 90}]
        polled.append(first_node_id)
        return [{"uid": first_user_id, "value": 30}]

    async def fake_record_user_stats_batched(all_node_params, usage_coefficients):
        raise OperationalError("stmt", {}, _ConnectionGone())

    context = {
        first_user_id: record_usages.UserUsageContext(admin_id=None, usage_epoch=0),
        second_user_id: record_usages.UserUsageContext(admin_id=None, usage_epoch=0),
    }

    monkeypatch.setattr(
        record_usages.node_manager,
        "get_healthy_nodes",
        AsyncMock(
            return_value=[
                (first_node_id, _StubNode(first_node_id)),
                (second_node_id, _StubNode(second_node_id)),
            ]
        ),
    )
    monkeypatch.setattr(record_usages, "get_users_stats", fake_get_users_stats)
    monkeypatch.setattr(record_usages, "current_usage_epochs", AsyncMock(return_value={uid: 0 for uid in context}))
    monkeypatch.setattr(record_usages, "load_user_usage_context", AsyncMock(return_value=context))
    monkeypatch.setattr(record_usages, "record_user_stats_batched", fake_record_user_stats_batched)
    monkeypatch.setattr(
        record_usages,
        "apply_fenced_user_usage",
        AsyncMock(return_value=record_usages.FencedUsageOutcome(0, 0, [], 0)),
    )
    monkeypatch.setattr(record_usages.usage_settings, "disable_recording_node_usage", False)
    monkeypatch.setattr(record_usages, "USAGE_PERSIST_MAX_VOLATILE_S", 0.05)

    with pytest.raises(OperationalError):
        await record_usages._record_user_usages_impl()

    assert polled == [first_node_id]


@pytest.mark.asyncio
async def test_lock_contention_does_not_stop_polling_the_remaining_nodes(monkeypatch: pytest.MonkeyPatch):
    first_node_id, second_node_id = 63, 64
    first_user_id, second_user_id = 403, 404
    polled: list[int] = []
    release = asyncio.Event()

    async def fake_get_users_stats(node, node_id=None):
        if node.node_id == second_node_id:
            try:
                await asyncio.wait_for(release.wait(), timeout=3.0)
            except TimeoutError:
                pass
            polled.append(second_node_id)
            return [{"uid": second_user_id, "value": 90}]
        polled.append(first_node_id)
        return [{"uid": first_user_id, "value": 30}]

    async def fake_record_user_stats_batched(all_node_params, usage_coefficients):
        release.set()
        if first_node_id in all_node_params:
            raise OperationalError("stmt", {}, _LockWaitTimeout())

    context = {
        first_user_id: record_usages.UserUsageContext(admin_id=None, usage_epoch=0),
        second_user_id: record_usages.UserUsageContext(admin_id=None, usage_epoch=0),
    }

    monkeypatch.setattr(
        record_usages.node_manager,
        "get_healthy_nodes",
        AsyncMock(
            return_value=[
                (first_node_id, _StubNode(first_node_id)),
                (second_node_id, _StubNode(second_node_id)),
            ]
        ),
    )
    monkeypatch.setattr(record_usages, "get_users_stats", fake_get_users_stats)
    monkeypatch.setattr(record_usages, "current_usage_epochs", AsyncMock(return_value={uid: 0 for uid in context}))
    monkeypatch.setattr(record_usages, "load_user_usage_context", AsyncMock(return_value=context))
    monkeypatch.setattr(record_usages, "record_user_stats_batched", fake_record_user_stats_batched)
    monkeypatch.setattr(
        record_usages,
        "apply_fenced_user_usage",
        AsyncMock(return_value=record_usages.FencedUsageOutcome(0, 0, [], 0)),
    )
    monkeypatch.setattr(record_usages.usage_settings, "disable_recording_node_usage", False)
    monkeypatch.setattr(record_usages, "USAGE_PERSIST_MAX_VOLATILE_S", 0.05)

    with pytest.raises(OperationalError):
        await record_usages._record_user_usages_impl()

    assert polled == [first_node_id, second_node_id]


@pytest.mark.asyncio
async def test_job_duration_is_visible_at_info_level(monkeypatch: pytest.MonkeyPatch, caplog):
    node_id = 81
    user_id = 501

    async def fake_get_users_stats(node, node_id=None):
        return [{"uid": user_id, "value": 12}]

    context = {user_id: record_usages.UserUsageContext(admin_id=None, usage_epoch=0)}

    monkeypatch.setattr(
        record_usages.node_manager, "get_healthy_nodes", AsyncMock(return_value=[(node_id, _StubNode(node_id))])
    )
    monkeypatch.setattr(record_usages, "get_users_stats", fake_get_users_stats)
    monkeypatch.setattr(record_usages, "current_usage_epochs", AsyncMock(return_value={user_id: 0}))
    monkeypatch.setattr(record_usages, "load_user_usage_context", AsyncMock(return_value=context))
    monkeypatch.setattr(record_usages, "record_user_stats_batched", AsyncMock())
    monkeypatch.setattr(
        record_usages,
        "apply_fenced_user_usage",
        AsyncMock(return_value=record_usages.FencedUsageOutcome(1, 0, [], 0)),
    )
    monkeypatch.setattr(record_usages.usage_settings, "disable_recording_node_usage", False)

    caplog.set_level(logging.INFO, logger="record-usages")
    await record_usages._record_user_usages_impl()

    info_records = [
        record for record in caplog.records if record.name == "record-usages" and record.levelno == logging.INFO
    ]
    assert any("User usage recording completed in" in record.getMessage() for record in info_records)


@pytest.mark.asyncio
async def test_a_lock_contention_storm_stops_polling_after_the_failure_cap(monkeypatch: pytest.MonkeyPatch):
    node_ids = [91, 92, 93]
    user_ids = [601, 602, 603]
    polled: list[int] = []
    gate = asyncio.Event()
    never = asyncio.Event()

    async def fake_get_users_stats(node, node_id=None):
        index = node_ids.index(node.node_id)
        if index == 1:
            try:
                await asyncio.wait_for(gate.wait(), timeout=3.0)
            except TimeoutError:
                pass
        elif index == 2:
            try:
                await asyncio.wait_for(never.wait(), timeout=3.0)
            except TimeoutError:
                pass
        polled.append(node.node_id)
        return [{"uid": user_ids[index], "value": 10}]

    async def fake_record_user_stats_batched(all_node_params, usage_coefficients):
        gate.set()
        raise OperationalError("stmt", {}, _LockWaitTimeout())

    context = {uid: record_usages.UserUsageContext(admin_id=None, usage_epoch=0) for uid in user_ids}

    monkeypatch.setattr(
        record_usages.node_manager,
        "get_healthy_nodes",
        AsyncMock(return_value=[(node_id, _StubNode(node_id)) for node_id in node_ids]),
    )
    monkeypatch.setattr(record_usages, "get_users_stats", fake_get_users_stats)
    monkeypatch.setattr(record_usages, "current_usage_epochs", AsyncMock(return_value={uid: 0 for uid in user_ids}))
    monkeypatch.setattr(record_usages, "load_user_usage_context", AsyncMock(return_value=context))
    monkeypatch.setattr(record_usages, "record_user_stats_batched", fake_record_user_stats_batched)
    monkeypatch.setattr(
        record_usages,
        "apply_fenced_user_usage",
        AsyncMock(return_value=record_usages.FencedUsageOutcome(0, 0, [], 0)),
    )
    monkeypatch.setattr(record_usages.usage_settings, "disable_recording_node_usage", False)
    monkeypatch.setattr(record_usages, "USAGE_PERSIST_MAX_VOLATILE_S", 0.05)

    with pytest.raises(OperationalError):
        await record_usages._record_user_usages_impl()

    assert polled == node_ids[: record_usages.USAGE_PERSIST_MAX_CONSECUTIVE_FAILURES]


@pytest.mark.asyncio
async def test_a_successful_cohort_resets_the_consecutive_failure_counter(monkeypatch: pytest.MonkeyPatch):
    node_ids = [94, 95, 96, 97]
    user_ids = [701, 702, 703, 704]
    polled: list[int] = []
    gates = {node_id: asyncio.Event() for node_id in node_ids[1:]}
    persist_calls = {"n": 0}

    async def fake_get_users_stats(node, node_id=None):
        gate = gates.get(node.node_id)
        if gate is not None:
            try:
                await asyncio.wait_for(gate.wait(), timeout=3.0)
            except TimeoutError:
                pass
        polled.append(node.node_id)
        return [{"uid": user_ids[node_ids.index(node.node_id)], "value": 10}]

    async def fake_record_user_stats_batched(all_node_params, usage_coefficients):
        persist_calls["n"] += 1
        call = persist_calls["n"]
        if call < len(node_ids):
            gates[node_ids[call]].set()
        if call in (1, 3):
            raise OperationalError("stmt", {}, _LockWaitTimeout())

    context = {uid: record_usages.UserUsageContext(admin_id=None, usage_epoch=0) for uid in user_ids}

    monkeypatch.setattr(
        record_usages.node_manager,
        "get_healthy_nodes",
        AsyncMock(return_value=[(node_id, _StubNode(node_id)) for node_id in node_ids]),
    )
    monkeypatch.setattr(record_usages, "get_users_stats", fake_get_users_stats)
    monkeypatch.setattr(record_usages, "current_usage_epochs", AsyncMock(return_value={uid: 0 for uid in user_ids}))
    monkeypatch.setattr(record_usages, "load_user_usage_context", AsyncMock(return_value=context))
    monkeypatch.setattr(record_usages, "record_user_stats_batched", fake_record_user_stats_batched)
    monkeypatch.setattr(
        record_usages,
        "apply_fenced_user_usage",
        AsyncMock(return_value=record_usages.FencedUsageOutcome(0, 0, [], 0)),
    )
    monkeypatch.setattr(record_usages.usage_settings, "disable_recording_node_usage", False)
    monkeypatch.setattr(record_usages, "USAGE_PERSIST_MAX_VOLATILE_S", 0.05)

    with pytest.raises(OperationalError):
        await record_usages._record_user_usages_impl()

    assert polled == node_ids
