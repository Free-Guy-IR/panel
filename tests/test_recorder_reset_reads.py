from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import OperationalError

from app.jobs import record_usages
from tests.test_recorder_harness import (  # noqa: F401
    CountingNode,
    recorder_db_fixture,
    seed_users_and_nodes,
    usage_snapshot,
)


class _ConnectionGone(Exception):
    args = (2003, "Can't connect to MySQL server")


def _fail_first_persist(
    monkeypatch: pytest.MonkeyPatch, delay: float = 0.0, after_partial_write: bool = False
) -> dict[str, int]:
    real_persist = record_usages._persist_collected_usage
    calls = {"n": 0}

    async def flaky_persist(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            await asyncio.sleep(delay)
            if after_partial_write and kwargs.get("progress") is not None:
                kwargs["progress"].writes_started = True
            raise OperationalError("stmt", {}, _ConnectionGone())
        return await real_persist(*args, **kwargs)

    monkeypatch.setattr(record_usages, "_persist_collected_usage", flaky_persist)
    return calls


def _install(monkeypatch: pytest.MonkeyPatch, nodes: list[CountingNode]) -> None:
    monkeypatch.setattr(
        record_usages.node_manager,
        "get_healthy_nodes",
        AsyncMock(return_value=[(node.node_id, node) for node in nodes]),
    )


@pytest.mark.asyncio
async def test_a_node_already_reset_is_not_cancelled_when_persistence_stops(
    monkeypatch: pytest.MonkeyPatch, recorder_db
):
    _, user_ids, node_ids = await seed_users_and_nodes(recorder_db, user_count=3, node_count=3)
    fast = CountingNode(node_ids[0], {user_ids[0]: 300})
    in_flight = CountingNode(node_ids[1], {user_ids[1]: 700}, latency=0.3)
    queued = CountingNode(node_ids[2], {user_ids[2]: 50})
    _install(monkeypatch, [fast, in_flight, queued])
    monkeypatch.setattr(record_usages, "API_SEM", asyncio.Semaphore(1))
    monkeypatch.setattr(record_usages, "USAGE_PERSIST_MAX_VOLATILE_S", 0.05)
    _fail_first_persist(monkeypatch)

    with pytest.raises(OperationalError):
        await record_usages._record_user_usages_impl()

    assert in_flight.reads == 1
    assert in_flight.cancelled_reads == 0
    assert queued.reads == 0

    await record_usages._record_user_usages_impl()

    billed, charted = await usage_snapshot(recorder_db, user_ids)
    assert billed == {user_ids[0]: 300, user_ids[1]: 700, user_ids[2]: 50}
    assert charted == billed


@pytest.mark.asyncio
async def test_a_node_read_during_a_failing_persist_is_kept_for_the_next_cycle(
    monkeypatch: pytest.MonkeyPatch, recorder_db
):
    _, user_ids, node_ids = await seed_users_and_nodes(recorder_db, user_count=2, node_count=2)
    fast = CountingNode(node_ids[0], {user_ids[0]: 400})
    slower = CountingNode(node_ids[1], {user_ids[1]: 900}, latency=0.1)
    _install(monkeypatch, [fast, slower])
    monkeypatch.setattr(record_usages, "USAGE_PERSIST_MAX_VOLATILE_S", 0.02)
    _fail_first_persist(monkeypatch, delay=0.3)

    with pytest.raises(OperationalError):
        await record_usages._record_user_usages_impl()

    assert slower.reads == 1
    assert slower.cancelled_reads == 0

    await record_usages._record_user_usages_impl()

    billed, charted = await usage_snapshot(recorder_db, user_ids)
    assert billed == {user_ids[0]: 400, user_ids[1]: 900}
    assert charted == billed
    assert slower.reads == 2


@pytest.mark.asyncio
async def test_kept_bytes_survive_a_cycle_whose_database_read_fails(monkeypatch: pytest.MonkeyPatch, recorder_db):
    _, user_ids, node_ids = await seed_users_and_nodes(recorder_db, user_count=2, node_count=2)
    fast = CountingNode(node_ids[0], {user_ids[0]: 10})
    slower = CountingNode(node_ids[1], {user_ids[1]: 250}, latency=0.1)
    _install(monkeypatch, [fast, slower])
    monkeypatch.setattr(record_usages, "USAGE_PERSIST_MAX_VOLATILE_S", 0.02)
    _fail_first_persist(monkeypatch, delay=0.3)

    with pytest.raises(OperationalError):
        await record_usages._record_user_usages_impl()

    real_epochs = record_usages.current_usage_epochs
    monkeypatch.setattr(
        record_usages,
        "current_usage_epochs",
        AsyncMock(side_effect=OperationalError("stmt", {}, _ConnectionGone())),
    )
    with pytest.raises(OperationalError):
        await record_usages._record_user_usages_impl()
    assert slower.reads == 1

    monkeypatch.setattr(record_usages, "current_usage_epochs", real_epochs)
    await record_usages._record_user_usages_impl()

    billed, charted = await usage_snapshot(recorder_db, user_ids)
    assert billed == {user_ids[0]: 10, user_ids[1]: 250}
    assert charted == billed


async def _leave_one_node_retained(monkeypatch: pytest.MonkeyPatch, recorder_db, raw_bytes: int):
    _, user_ids, node_ids = await seed_users_and_nodes(recorder_db, user_count=2, node_count=2)
    fast = CountingNode(node_ids[0], {user_ids[0]: 10})
    slower = CountingNode(node_ids[1], {user_ids[1]: raw_bytes}, latency=0.1)
    _install(monkeypatch, [fast, slower])
    monkeypatch.setattr(record_usages, "USAGE_PERSIST_MAX_VOLATILE_S", 0.02)
    _fail_first_persist(monkeypatch, delay=0.3, after_partial_write=True)
    with pytest.raises(OperationalError):
        await record_usages._record_user_usages_impl()
    assert len(record_usages._retained_cohorts) == 1
    return user_ids, fast, slower


@pytest.mark.asyncio
async def test_kept_bytes_outlive_a_failed_retry_that_wrote_nothing(monkeypatch: pytest.MonkeyPatch, recorder_db):
    user_ids, fast, slower = await _leave_one_node_retained(monkeypatch, recorder_db, 640)
    real_context = record_usages.load_user_usage_context
    context_calls = {"n": 0}

    async def context_down_once(uids):
        context_calls["n"] += 1
        if context_calls["n"] == 1:
            raise OperationalError("stmt", {}, _ConnectionGone())
        return await real_context(uids)

    monkeypatch.setattr(record_usages, "load_user_usage_context", context_down_once)

    with pytest.raises(OperationalError):
        await record_usages._record_user_usages_impl()
    assert (fast.reads, slower.reads) == (1, 1)
    assert [cohort.attempts for cohort in record_usages._retained_cohorts] == [1]

    await record_usages._record_user_usages_impl()

    billed, charted = await usage_snapshot(recorder_db, user_ids)
    assert billed[user_ids[1]] == 640
    assert charted[user_ids[1]] == 640
    assert record_usages._retained_cohorts == []


@pytest.mark.asyncio
async def test_kept_bytes_are_not_retried_once_a_write_started(monkeypatch: pytest.MonkeyPatch, recorder_db, caplog):
    user_ids, _, _ = await _leave_one_node_retained(monkeypatch, recorder_db, 330)
    real_apply = record_usages.apply_fenced_user_usage
    apply_calls = {"n": 0}

    async def billing_down_once(*args, **kwargs):
        apply_calls["n"] += 1
        if apply_calls["n"] == 1:
            raise OperationalError("stmt", {}, _ConnectionGone())
        return await real_apply(*args, **kwargs)

    monkeypatch.setattr(record_usages, "apply_fenced_user_usage", billing_down_once)

    with pytest.raises(OperationalError):
        await record_usages._record_user_usages_impl()
    assert record_usages._retained_cohorts == []
    assert "Gave up on 330 raw bytes" in caplog.text

    await record_usages._record_user_usages_impl()

    _, charted = await usage_snapshot(recorder_db, user_ids)
    assert charted[user_ids[1]] == 330


@pytest.mark.asyncio
async def test_kept_bytes_are_given_up_after_the_retry_limit(monkeypatch: pytest.MonkeyPatch, recorder_db, caplog):
    await _leave_one_node_retained(monkeypatch, recorder_db, 90)
    monkeypatch.setattr(record_usages, "USAGE_RETAINED_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(
        record_usages,
        "load_user_usage_context",
        AsyncMock(side_effect=OperationalError("stmt", {}, _ConnectionGone())),
    )

    for _ in range(2):
        with pytest.raises(OperationalError):
            await record_usages._record_user_usages_impl()

    assert record_usages._retained_cohorts == []
    assert "Gave up on 90 raw bytes" in caplog.text
    assert "the retry limit was reached" in caplog.text


def _load_context_down_once(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    real_context = record_usages.load_user_usage_context
    calls = {"n": 0}

    async def context_down_once(uids):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("stmt", {}, _ConnectionGone())
        return await real_context(uids)

    monkeypatch.setattr(record_usages, "load_user_usage_context", context_down_once)
    return calls


@pytest.mark.asyncio
async def test_a_fresh_cohort_that_failed_before_writing_is_recovered_exactly_once(
    monkeypatch: pytest.MonkeyPatch, recorder_db
):
    _, user_ids, node_ids = await seed_users_and_nodes(recorder_db, user_count=2, node_count=1)
    node = CountingNode(node_ids[0], {user_ids[0]: 300, user_ids[1]: 120})
    _install(monkeypatch, [node])
    _load_context_down_once(monkeypatch)
    billed_start, charted_start = await usage_snapshot(recorder_db, user_ids)

    with pytest.raises(OperationalError):
        await record_usages._record_user_usages_impl()
    assert node.reads == 1

    node.add(user_ids[0], 50)
    await record_usages._record_user_usages_impl()
    await record_usages._record_user_usages_impl()

    billed_end, charted_end = await usage_snapshot(recorder_db, user_ids)
    billed = {uid: billed_end[uid] - billed_start[uid] for uid in user_ids}
    charted = {uid: charted_end[uid] - charted_start[uid] for uid in user_ids}
    assert billed == {user_ids[0]: 350, user_ids[1]: 120}
    assert charted == billed
    assert record_usages._retained_cohorts == []


@pytest.mark.asyncio
async def test_a_fresh_cohort_whose_write_started_is_given_up(monkeypatch: pytest.MonkeyPatch, recorder_db, caplog):
    _, user_ids, node_ids = await seed_users_and_nodes(recorder_db, user_count=1, node_count=1)
    node = CountingNode(node_ids[0], {user_ids[0]: 210})
    _install(monkeypatch, [node])
    monkeypatch.setattr(
        record_usages,
        "apply_fenced_user_usage",
        AsyncMock(side_effect=OperationalError("stmt", {}, _ConnectionGone())),
    )

    with pytest.raises(OperationalError):
        await record_usages._record_user_usages_impl()

    assert record_usages._retained_cohorts == []
    assert "Gave up on 210 raw bytes" in caplog.text
    assert "a write had already started" in caplog.text


@pytest.mark.asyncio
async def test_a_fresh_cohort_cancelled_before_writing_is_kept(monkeypatch: pytest.MonkeyPatch, recorder_db):
    _, user_ids, node_ids = await seed_users_and_nodes(recorder_db, user_count=1, node_count=1)
    node = CountingNode(node_ids[0], {user_ids[0]: 480})
    _install(monkeypatch, [node])
    real_context = record_usages.load_user_usage_context
    entered = asyncio.Event()
    calls = {"n": 0}

    async def stalled_context(uids):
        calls["n"] += 1
        if calls["n"] == 1:
            entered.set()
            await asyncio.sleep(5)
        return await real_context(uids)

    monkeypatch.setattr(record_usages, "load_user_usage_context", stalled_context)

    tick = asyncio.ensure_future(record_usages._record_user_usages_impl())
    await asyncio.wait_for(entered.wait(), timeout=2.0)
    tick.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tick
    assert len(record_usages._retained_cohorts) == 1

    await record_usages._record_user_usages_impl()

    billed, charted = await usage_snapshot(recorder_db, user_ids)
    assert billed[user_ids[0]] == charted[user_ids[0]] == 480
    assert node.reads == 2
