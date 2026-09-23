from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.scheduler import scheduler as app_scheduler


def test_scheduler_defaults_tolerate_a_late_start_without_overlapping():
    defaults = app_scheduler._job_defaults

    assert defaults["misfire_grace_time"] == 30
    assert defaults["max_instances"] == 1
    assert defaults["coalesce"] is True


@pytest.mark.asyncio
async def test_a_job_that_fires_ten_seconds_late_still_runs():
    probe = AsyncIOScheduler(job_defaults=dict(app_scheduler._job_defaults), timezone="UTC")
    ran = asyncio.Event()

    async def late_job():
        ran.set()

    probe.add_job(late_job, "date", run_date=datetime.now(UTC) - timedelta(seconds=10), id="late-probe")
    probe.start()
    waiter = asyncio.ensure_future(ran.wait())
    try:
        await asyncio.wait({waiter}, timeout=3.0)
    finally:
        waiter.cancel()
        probe.shutdown(wait=False)

    assert ran.is_set()
