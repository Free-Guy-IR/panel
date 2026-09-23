import asyncio
import importlib
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app import app_factory, lifecycle
from app.fork import notification_spool
from app.jobs import record_usages
from app.notification import queue_manager
from app.notification.nats_queue import InMemoryNotificationQueue

DOCKER_STOP_TIMEOUT_S = 10.0


def test_the_budget_leaves_margin_under_the_docker_stop_timeout():
    assert lifecycle.SHUTDOWN_BUDGET_S + 0.5 <= DOCKER_STOP_TIMEOUT_S
    assert 0 < lifecycle.SHUTDOWN_FINAL_RESERVE_S < lifecycle.SHUTDOWN_BUDGET_S


def test_create_app_registers_the_usage_drain_first_and_the_notification_spool_last():
    app_factory.create_app()
    names = [getattr(func, "__qualname__", "") for func in lifecycle.shutdown_functions]
    final = [getattr(func, "__qualname__", "") for func in lifecycle.final_shutdown_functions]

    assert names[0].endswith("_stop_scheduler_and_leader")
    assert "send_pending_notifications_before_shutdown" in names
    assert final[-1] == "persist_notification_backlog"


@pytest.mark.asyncio
async def test_the_usage_drain_is_given_only_what_is_left_of_the_budget(monkeypatch: pytest.MonkeyPatch):
    assert app_factory._usage_drain_timeout() == record_usages.USAGE_SHUTDOWN_GRACE_S
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(lifecycle, "_shutdown_deadline", loop.time() + 3.0)
    assert 2.0 < app_factory._usage_drain_timeout() <= 3.0 - app_factory.USAGE_DRAIN_TAIL_S
    monkeypatch.setattr(lifecycle, "_shutdown_deadline", loop.time() + 60.0)
    assert app_factory._usage_drain_timeout() == record_usages.USAGE_SHUTDOWN_GRACE_S
    main_phase = lifecycle.SHUTDOWN_BUDGET_S - lifecycle.SHUTDOWN_FINAL_RESERVE_S
    monkeypatch.setattr(lifecycle, "_shutdown_deadline", loop.time() + main_phase)
    assert app_factory._usage_drain_timeout() == pytest.approx(
        min(record_usages.USAGE_SHUTDOWN_GRACE_S, lifecycle.SHUTDOWN_BUDGET_S - 1.0), abs=0.01
    )


@pytest.mark.asyncio
async def test_a_slow_notification_flush_cannot_push_shutdown_past_the_budget_and_runs_after_the_usage_drain(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    loop = asyncio.get_running_loop()
    events: list[tuple[str, float]] = []
    scheduler = AsyncIOScheduler(timezone="UTC")
    monkeypatch.setattr(importlib.import_module("app.scheduler"), "scheduler", scheduler)
    leader = importlib.import_module("app.nats.leader")
    monkeypatch.setattr(leader, "set_on_leadership_lost", Mock())
    monkeypatch.setattr(leader, "start_job_leader", AsyncMock(return_value=True))
    monkeypatch.setattr(leader, "stop_job_leader", AsyncMock())
    monkeypatch.setattr(lifecycle, "startup_functions", [])
    monkeypatch.setattr(lifecycle, "shutdown_functions", [])
    monkeypatch.setattr(lifecycle, "final_shutdown_functions", [])
    monkeypatch.setattr(notification_spool, "SPOOL_PATH", tmp_path / "notification_spool.json")
    backlog = InMemoryNotificationQueue()
    monkeypatch.setattr(queue_manager, "queue_instance", backlog)
    monkeypatch.setattr(queue_manager, "webhook_queue_instance", InMemoryNotificationQueue())

    async def slow_notification_flush():
        events.append(("flush started", loop.time()))
        await asyncio.sleep(60)

    async def later_teardown():
        events.append(("later teardown", loop.time()))

    lifecycle.on_shutdown(slow_notification_flush)
    lifecycle.on_shutdown(later_teardown)
    app_factory._register_scheduler_hooks()
    monkeypatch.setattr(lifecycle, "startup_functions", [])

    tick_started = asyncio.Event()

    async def tick():
        tick_started.set()
        await asyncio.sleep(1.0)
        events.append(("usage tick persisted", loop.time()))

    async def usage_job():
        await record_usages._await_usage_job("record_user_usages", tick, 10, "JOB_RECORD_USER_USAGES_INTERVAL")

    await backlog.enqueue(queue_manager.TelegramNotification(message="unsent", chat_id=1).model_dump())

    async with lifecycle.lifespan(None):
        scheduler.start()
        scheduler.add_job(usage_job, "date", run_date=datetime.now(UTC), id="usage-tick")
        await asyncio.wait_for(tick_started.wait(), timeout=2.0)
        shutdown_started = loop.time()

    total = loop.time() - shutdown_started
    order = [name for name, _ in events]

    assert order[:2] == ["usage tick persisted", "flush started"]
    assert all(stamp <= shutdown_started + lifecycle.SHUTDOWN_BUDGET_S for _, stamp in events)
    assert total <= lifecycle.SHUTDOWN_BUDGET_S + 0.25
    assert total < DOCKER_STOP_TIMEOUT_S
    saved = json.loads((tmp_path / "notification_spool.json").read_text())["notifications"]
    assert [entry["item"]["message"] for entry in saved] == ["unsent"]


@pytest.mark.asyncio
async def test_a_callback_that_swallows_its_cancellation_still_cannot_hold_shutdown_past_the_budget(
    monkeypatch: pytest.MonkeyPatch,
):
    loop = asyncio.get_running_loop()
    ran: list[str] = []
    released = asyncio.Event()
    monkeypatch.setattr(lifecycle, "SHUTDOWN_BUDGET_S", 0.6)
    monkeypatch.setattr(lifecycle, "SHUTDOWN_FINAL_RESERVE_S", 0.2)

    async def stubborn():
        while not released.is_set():
            try:
                await released.wait()
            except asyncio.CancelledError:
                continue

    async def final_step():
        ran.append("final")

    monkeypatch.setattr(lifecycle, "startup_functions", [])
    monkeypatch.setattr(lifecycle, "shutdown_functions", [stubborn])
    monkeypatch.setattr(lifecycle, "final_shutdown_functions", [final_step])

    try:
        async with lifecycle.lifespan(None):
            started = loop.time()
        elapsed = loop.time() - started
    finally:
        released.set()
        await asyncio.sleep(0)

    assert elapsed <= 0.6 + 0.1
    assert ran == ["final"]
