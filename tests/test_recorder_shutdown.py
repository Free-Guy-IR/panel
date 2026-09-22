from __future__ import annotations

import asyncio
import importlib
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app import app_factory, lifecycle
from app.jobs import record_usages
from tests.test_recorder_harness import (  # noqa: F401
    CountingNode,
    recorder_db_fixture,
    seed_users_and_nodes,
    usage_snapshot,
)


def _install_scheduler_hooks(monkeypatch: pytest.MonkeyPatch) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")
    monkeypatch.setattr(importlib.import_module("app.scheduler"), "scheduler", scheduler)
    leader = importlib.import_module("app.nats.leader")
    monkeypatch.setattr(leader, "set_on_leadership_lost", Mock())
    monkeypatch.setattr(leader, "start_job_leader", AsyncMock(return_value=True))
    monkeypatch.setattr(leader, "stop_job_leader", AsyncMock())
    monkeypatch.setattr(lifecycle, "startup_functions", [])
    monkeypatch.setattr(lifecycle, "shutdown_functions", [])
    app_factory._register_scheduler_hooks()
    monkeypatch.setattr(lifecycle, "startup_functions", [])
    return scheduler


@pytest.mark.asyncio
async def test_shutdown_lets_an_in_flight_usage_tick_persist_what_it_already_read(
    monkeypatch: pytest.MonkeyPatch, recorder_db
):
    _, user_ids, node_ids = await seed_users_and_nodes(recorder_db, user_count=2, node_count=2)
    fast = CountingNode(node_ids[0], {user_ids[0]: 400})
    slow = CountingNode(node_ids[1], {user_ids[1]: 600}, latency=0.3)
    monkeypatch.setattr(
        record_usages.node_manager,
        "get_healthy_nodes",
        AsyncMock(return_value=[(fast.node_id, fast), (slow.node_id, slow)]),
    )
    scheduler = _install_scheduler_hooks(monkeypatch)

    async with lifecycle.lifespan(None):
        scheduler.start()
        scheduler.add_job(record_usages.record_user_usages, "date", run_date=datetime.now(UTC), id="usage-tick")
        await asyncio.wait_for(slow.read_issued.wait(), timeout=2.0)
        await asyncio.sleep(0.05)

    billed, charted = await usage_snapshot(recorder_db, user_ids)
    assert billed == {user_ids[0]: 400, user_ids[1]: 600}
    assert charted == billed
    assert slow.cancelled_reads == 0


@pytest.mark.asyncio
async def test_shutdown_waits_for_a_usage_tick_only_up_to_the_grace_period(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(record_usages, "_usage_job_runs", set())
    monkeypatch.setattr(record_usages, "USAGE_SHUTDOWN_GRACE_S", 0.2)
    release = asyncio.Event()

    async def stuck_impl():
        await release.wait()

    job = asyncio.ensure_future(
        record_usages._await_usage_job("record_user_usages", stuck_impl, 10, "JOB_RECORD_USER_USAGES_INTERVAL")
    )
    await asyncio.sleep(0.01)
    job.cancel()

    started = time.monotonic()
    await record_usages.drain_usage_jobs()
    waited = time.monotonic() - started

    assert 0.15 <= waited < 1.0
    release.set()
    await asyncio.wait_for(job, timeout=1.0)
    assert not record_usages._usage_job_runs


@pytest.mark.asyncio
async def test_a_failing_shutdown_callback_does_not_skip_the_rest(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []

    async def broken():
        calls.append("broken")
        raise RuntimeError("shutdown callback failed")

    def after():
        calls.append("after")

    monkeypatch.setattr(lifecycle, "startup_functions", [])
    monkeypatch.setattr(lifecycle, "shutdown_functions", [broken, after])

    async with lifecycle.lifespan(None):
        pass

    assert calls == ["broken", "after"]


@pytest.mark.asyncio
async def test_thread_pool_shutdown_does_not_block_the_event_loop(monkeypatch: pytest.MonkeyPatch):
    pool = ThreadPoolExecutor(max_workers=1)
    busy = pool.submit(time.sleep, 0.3)
    monkeypatch.setattr(record_usages, "_thread_pool", pool)
    ticks = {"n": 0}

    async def ticker():
        while True:
            ticks["n"] += 1
            await asyncio.sleep(0.01)

    ticking = asyncio.ensure_future(ticker())
    await asyncio.sleep(0)
    before = ticks["n"]
    await record_usages._cleanup_thread_pool()
    during = ticks["n"] - before
    ticking.cancel()

    assert busy.done()
    assert record_usages._thread_pool is None
    assert during >= 5
