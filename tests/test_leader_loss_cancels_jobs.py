import asyncio
import importlib
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app import app_factory, lifecycle
from app.jobs import record_usages


def _install(monkeypatch: pytest.MonkeyPatch) -> tuple[AsyncIOScheduler, list]:
    scheduler = AsyncIOScheduler(timezone="UTC")
    monkeypatch.setattr(importlib.import_module("app.scheduler"), "scheduler", scheduler)
    leader = importlib.import_module("app.nats.leader")
    callbacks: list = []
    monkeypatch.setattr(leader, "set_on_leadership_lost", callbacks.append)
    monkeypatch.setattr(leader, "start_job_leader", AsyncMock(return_value=True))
    monkeypatch.setattr(leader, "stop_job_leader", AsyncMock())
    monkeypatch.setattr(leader, "HEARTBEAT_INTERVAL", 3600.0)
    monkeypatch.setattr(lifecycle, "startup_functions", [])
    monkeypatch.setattr(lifecycle, "shutdown_functions", [])
    monkeypatch.setattr(lifecycle, "final_shutdown_functions", [], raising=False)
    app_factory._register_scheduler_hooks()
    return scheduler, callbacks


async def _stop(scheduler: AsyncIOScheduler) -> None:
    for func in list(lifecycle.shutdown_functions):
        if getattr(func, "__qualname__", "").endswith("_stop_scheduler_and_leader"):
            await func()
    if scheduler.running:
        scheduler.shutdown(wait=False)


@pytest.mark.asyncio
async def test_a_job_running_on_a_worker_that_lost_leadership_stops_writing(monkeypatch: pytest.MonkeyPatch):
    scheduler, callbacks = _install(monkeypatch)
    writes: list[float] = []
    started = asyncio.Event()

    async def review_like_job():
        started.set()
        while True:
            writes.append(asyncio.get_running_loop().time())
            await asyncio.sleep(0.02)

    scheduler.start()
    scheduler.add_job(review_like_job, "date", run_date=datetime.now(UTC), id="review-like")
    try:
        await asyncio.wait_for(started.wait(), timeout=2.0)
        await asyncio.sleep(0.05)

        assert len(callbacks) == 1
        await callbacks[0]()
        lost_at = asyncio.get_running_loop().time()
        await asyncio.sleep(0.2)
    finally:
        await _stop(scheduler)

    after_loss = [stamp for stamp in writes if stamp > lost_at + 0.01]
    assert after_loss == [], (
        f"the job kept writing {len(after_loss)} times after this worker lost the leader lease; "
        "the new leader runs the same job concurrently, so the lease fences nothing"
    )


@pytest.mark.asyncio
async def test_a_usage_tick_that_already_read_counters_still_finishes_after_leadership_loss(
    monkeypatch: pytest.MonkeyPatch,
):
    scheduler, callbacks = _install(monkeypatch)
    persisted = asyncio.Event()
    reading = asyncio.Event()
    release = asyncio.Event()

    async def tick():
        reading.set()
        await release.wait()
        persisted.set()

    async def usage_job():
        await record_usages._await_usage_job("record_user_usages", tick, 10, "JOB_RECORD_USER_USAGES_INTERVAL")

    scheduler.start()
    scheduler.add_job(usage_job, "date", run_date=datetime.now(UTC), id="usage-like")
    try:
        await asyncio.wait_for(reading.wait(), timeout=2.0)
        await callbacks[0]()
        release.set()
        await asyncio.wait_for(persisted.wait(), timeout=2.0)
    finally:
        await _stop(scheduler)

    assert persisted.is_set()
