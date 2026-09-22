from __future__ import annotations

import asyncio
import importlib
import inspect
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.exc import OperationalError

from app import app_factory, lifecycle
from app.jobs import record_usages
from tests.test_recorder_harness import (  # noqa: F401
    CountingNode,
    recorder_db_fixture,
    seed_users_and_nodes,
    usage_snapshot,
)


class _ConnectionGone(Exception):
    args = (2003, "Can't connect to MySQL server")


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


async def _keep_one_cohort(monkeypatch: pytest.MonkeyPatch, recorder_db, raw_bytes: int) -> list[int]:
    _, user_ids, node_ids = await seed_users_and_nodes(recorder_db, user_count=2, node_count=2)
    fast = CountingNode(node_ids[0], {user_ids[0]: 5})
    slower = CountingNode(node_ids[1], {user_ids[1]: raw_bytes}, latency=0.1)
    monkeypatch.setattr(
        record_usages.node_manager,
        "get_healthy_nodes",
        AsyncMock(return_value=[(fast.node_id, fast), (slower.node_id, slower)]),
    )
    monkeypatch.setattr(record_usages, "USAGE_PERSIST_MAX_VOLATILE_S", 0.02)
    real_persist = record_usages._persist_collected_usage
    calls = {"n": 0}

    async def first_persist_fails(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            await asyncio.sleep(0.3)
            raise OperationalError("stmt", {}, _ConnectionGone())
        return await real_persist(*args, **kwargs)

    monkeypatch.setattr(record_usages, "_persist_collected_usage", first_persist_fails)
    with pytest.raises(OperationalError):
        await record_usages._record_user_usages_impl()
    assert len(record_usages._retained_cohorts) == 1
    return user_ids


@pytest.mark.asyncio
async def test_shutdown_writes_usage_kept_from_a_failed_tick(monkeypatch: pytest.MonkeyPatch, recorder_db):
    user_ids = await _keep_one_cohort(monkeypatch, recorder_db, 770)

    await record_usages.drain_usage_jobs()

    billed, charted = await usage_snapshot(recorder_db, user_ids)
    assert billed[user_ids[1]] == 770
    assert charted[user_ids[1]] == 770
    assert record_usages._retained_cohorts == []


@pytest.mark.asyncio
async def test_shutdown_reports_kept_usage_it_could_not_write(monkeypatch: pytest.MonkeyPatch, recorder_db, caplog):
    await _keep_one_cohort(monkeypatch, recorder_db, 515)
    monkeypatch.setattr(
        record_usages,
        "load_user_usage_context",
        AsyncMock(side_effect=OperationalError("stmt", {}, _ConnectionGone())),
    )

    await record_usages.drain_usage_jobs()

    assert "Shutting down with 515 raw bytes" in caplog.text


def test_node_teardown_is_registered_after_the_usage_drain():
    script = (
        "from app.app_factory import create_app\n"
        "create_app()\n"
        "from app import lifecycle\n"
        "for func in lifecycle.shutdown_functions:\n"
        "    print('SHUTDOWN', getattr(func, '__qualname__', repr(func)))\n"
    )
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    names = [line.split(" ", 1)[1] for line in result.stdout.splitlines() if line.startswith("SHUTDOWN ")]

    assert "_register_scheduler_hooks.<locals>._stop_scheduler_and_leader" in names
    node_teardown = ("shutdown_nodes", "_stop_node_loops", "shutdown_bridge_memory")
    assert not set(node_teardown) & set(names)
    startup_source = inspect.getsource(importlib.import_module("app.jobs.node_checker").initialize_nodes)
    for name in node_teardown:
        assert f"on_shutdown({name})" in startup_source


@pytest.mark.asyncio
async def test_an_expired_grace_does_not_flush_kept_usage_under_a_running_tick(monkeypatch: pytest.MonkeyPatch):
    kept = record_usages.UsageCohort({1: [{"uid": 1, "value": 9}]}, {1: 1.0}, {1: 0})
    monkeypatch.setattr(record_usages, "_usage_job_runs", set())
    monkeypatch.setattr(record_usages, "_retained_cohorts", [kept])
    monkeypatch.setattr(record_usages, "USAGE_SHUTDOWN_GRACE_S", 0.1)
    flush = AsyncMock()
    monkeypatch.setattr(record_usages, "_flush_retained_cohorts", flush)
    release = asyncio.Event()

    async def stuck_impl():
        await release.wait()

    job = asyncio.ensure_future(
        record_usages._await_usage_job("record_user_usages", stuck_impl, 10, "JOB_RECORD_USER_USAGES_INTERVAL")
    )
    await asyncio.sleep(0.01)

    await record_usages.drain_usage_jobs()

    flush.assert_not_awaited()
    assert record_usages._retained_cohorts == [kept]
    release.set()
    await asyncio.wait_for(job, timeout=1.0)


@pytest.mark.asyncio
async def test_a_second_cancellation_releases_the_job_wrapper(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(record_usages, "_usage_job_runs", set())
    release = asyncio.Event()

    async def stuck_impl():
        await release.wait()

    job = asyncio.ensure_future(
        record_usages._await_usage_job("record_user_usages", stuck_impl, 10, "JOB_RECORD_USER_USAGES_INTERVAL")
    )
    await asyncio.sleep(0.01)
    job.cancel()
    await asyncio.sleep(0.01)
    assert not job.done()
    (run,) = record_usages._usage_job_runs

    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(job, timeout=1.0)
    assert not run.done()

    release.set()
    await asyncio.wait_for(run, timeout=1.0)
