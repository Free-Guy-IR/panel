import contextlib
import logging
import time
from types import SimpleNamespace

import pytest
from aiohttp import web

from app.fork import notification_spool
from app.models.settings import NotificationSettings
from app.notification import client as notification_client, nats_queue, queue_manager
from app.notification.nats_queue import QUEUE_MAX_MESSAGES, InMemoryNotificationQueue
from tests.test_notification_delivery import assert_drained, make_nats_queue, use_telegram

HOUR = 3600.0
DROPPED = "Notification dropped after 24 hours of rate limiting ({})"
QUEUE_FULL = "Notification queue is full, dropping a discord notification"
BACKENDS = pytest.mark.parametrize("backend", ["memory", "nats"])


@pytest.fixture
def shifted_clock(monkeypatch):
    state = SimpleNamespace(offset=0.0)
    state.monotonic = lambda: time.monotonic() + state.offset
    monkeypatch.setattr(notification_client, "time", SimpleNamespace(time=lambda: time.time() + state.offset))
    monkeypatch.setattr(nats_queue, "time", SimpleNamespace(monotonic=state.monotonic))
    monkeypatch.setattr(notification_client, "_rate_limits", {})
    return state


def _use_queue(backend, clock, monkeypatch, size=QUEUE_MAX_MESSAGES):
    queue = InMemoryNotificationQueue(maxsize=size) if backend == "memory" else make_nats_queue(clock, max_msgs=size)
    monkeypatch.setattr(queue_manager, "queue_instance", queue)
    return queue


class _Destinations:
    def __init__(self):
        self.limited: set[str] = set()
        self.seen: list[tuple[str, str, int]] = []

    async def handle(self, request):
        name = request.match_info["name"]
        status = 429 if name in self.limited else 204
        self.seen.append((name, (await request.json())["content"], status))
        if status == 429:
            return web.json_response({"retry_after": HOUR}, status=429)
        return web.Response(status=204)

    def contents(self, name, status):
        return [content for seen, content, answered in self.seen if (seen, answered) == (name, status)]


@contextlib.asynccontextmanager
async def _real_client_against(destinations, monkeypatch):
    application = web.Application()
    application.router.add_post("/{name}", destinations.handle)
    runner = web.AppRunner(application, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    stored = NotificationSettings.model_validate(
        {"notify_discord": True, "discord_webhook_url": "https://discord.com/api/webhooks/1/probe", "max_retries": 3}
    )

    async def settings():
        return stored

    monkeypatch.setattr(notification_client, "notification_settings", settings)
    monkeypatch.setattr(notification_client, "client", None)
    await notification_client.define_client()
    try:
        yield f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    finally:
        await notification_client.client.close()
        await runner.cleanup()


async def _due(queue) -> None:
    for _ in range(10):
        delivery = await queue.dequeue(timeout=0)
        if delivery is None:
            return
        await notification_client.process_notification(delivery)


async def _hours(queue, clock, hours: int) -> None:
    for _ in range(hours):
        clock.offset += HOUR + 1
        await _due(queue)


def _lines(caplog, prefix):
    return [record.getMessage() for record in caplog.records if record.getMessage().startswith(prefix)]


@BACKENDS
async def test_a_message_its_destination_rate_limits_for_a_day_is_dropped_with_one_log_line(
    backend, shifted_clock, monkeypatch, caplog
):
    queue = _use_queue(backend, shifted_clock, monkeypatch)
    destinations = _Destinations()
    destinations.limited.add("limited")
    caplog.set_level(logging.WARNING)
    async with _real_client_against(destinations, monkeypatch) as base:
        await queue_manager.enqueue_discord({"content": "probe"}, f"{base}/limited")
        await _due(queue)
        await _hours(queue, shifted_clock, 26)

    assert_drained(queue)
    assert _lines(caplog, "Notification dropped") == [DROPPED.format("discord")]
    assert destinations.contents("limited", 429) == ["probe"] * 25


@BACKENDS
async def test_a_destination_that_answers_again_gets_the_message_even_past_a_day(
    backend, shifted_clock, monkeypatch, caplog
):
    queue = _use_queue(backend, shifted_clock, monkeypatch)
    destinations = _Destinations()
    destinations.limited.add("limited")
    caplog.set_level(logging.WARNING)
    async with _real_client_against(destinations, monkeypatch) as base:
        await queue_manager.enqueue_discord({"content": "probe"}, f"{base}/limited")
        await _due(queue)
        await _hours(queue, shifted_clock, 23)
        destinations.limited.clear()
        await _hours(queue, shifted_clock, 1)

    assert_drained(queue)
    assert destinations.contents("limited", 204) == ["probe"]
    assert destinations.contents("limited", 429) == ["probe"] * 24
    assert _lines(caplog, "Notification dropped") == []


@BACKENDS
async def test_messages_held_behind_a_rate_limited_one_leave_with_it_and_free_the_queue(
    backend, shifted_clock, monkeypatch, caplog
):
    monkeypatch.setattr("app.fork.notification_rate_limit.rate_limit_share_settings.destination_share", 1.0)
    queue = _use_queue(backend, shifted_clock, monkeypatch, size=2)
    destinations = _Destinations()
    destinations.limited.add("limited")
    caplog.set_level(logging.WARNING)
    async with _real_client_against(destinations, monkeypatch) as base:
        await queue_manager.enqueue_discord({"content": "first"}, f"{base}/limited")
        await queue_manager.enqueue_discord({"content": "second"}, f"{base}/limited")
        await _due(queue)
        await _hours(queue, shifted_clock, 12)
        await queue_manager.enqueue_discord({"content": "refused"}, f"{base}/healthy")
        await _hours(queue, shifted_clock, 14)
        await queue_manager.enqueue_discord({"content": "accepted"}, f"{base}/healthy")
        await _due(queue)

    assert_drained(queue)
    assert destinations.contents("healthy", 204) == ["accepted"]
    assert _lines(caplog, QUEUE_FULL) == [QUEUE_FULL]
    assert _lines(caplog, "Notification dropped") == [DROPPED.format("discord")] * 2
    assert destinations.contents("limited", 429) == ["first"] * 25


@BACKENDS
async def test_a_telegram_chat_that_never_stops_rate_limiting_drops_its_message_after_a_day(
    backend, shifted_clock, monkeypatch, caplog
):
    queue = _use_queue(backend, shifted_clock, monkeypatch)
    api = use_telegram(monkeypatch, shifted_clock, max_retries=3)
    api.floods[-100123] = [HOUR] * 40
    caplog.set_level(logging.WARNING)
    await queue_manager.enqueue_telegram("probe", chat_id=-100123)
    await _due(queue)
    await _hours(queue, shifted_clock, 26)

    assert_drained(queue)
    assert _lines(caplog, "Notification dropped") == [DROPPED.format("telegram")]
    assert (api.requests[-100123], api.texts(-100123)) == (25, [])


async def test_the_time_a_rate_limit_began_survives_a_restart_through_the_spool(
    shifted_clock, monkeypatch, caplog, tmp_path
):
    monkeypatch.setattr(notification_spool, "SPOOL_PATH", tmp_path / "notification_spool.json")
    monkeypatch.setattr(queue_manager, "webhook_queue_instance", InMemoryNotificationQueue())
    before = _use_queue("memory", shifted_clock, monkeypatch)
    destinations = _Destinations()
    destinations.limited.add("limited")
    caplog.set_level(logging.WARNING)
    async with _real_client_against(destinations, monkeypatch) as base:
        await queue_manager.enqueue_discord({"content": "probe"}, f"{base}/limited")
        await _due(before)
        await _hours(before, shifted_clock, 12)
        await notification_spool.persist_notification_backlog()
        monkeypatch.setattr(notification_client, "_rate_limits", {})
        after = _use_queue("memory", shifted_clock, monkeypatch)
        await notification_spool.restore_notification_backlog()
        await _hours(after, shifted_clock, 14)

    assert before.drain_nowait() == []
    assert_drained(after)
    assert _lines(caplog, "Notification dropped") == [DROPPED.format("discord")]
    assert destinations.contents("limited", 429) == ["probe"] * 25
