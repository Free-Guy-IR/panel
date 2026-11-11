import asyncio
import contextlib
import logging
import time
from types import SimpleNamespace

import pytest
from aiohttp import web
from nats.js.api import RetentionPolicy, StreamConfig

from app.models.settings import NotificationSettings
from app.notification import client as notification_client, nats_queue, queue_manager
from app.notification.nats_queue import QUEUE_MAX_MESSAGES, InMemoryNotificationQueue, NatsNotificationQueue
from tests.test_notification_delivery import (
    STREAM,
    SUBJECT,
    _queued,
    _startup_js,
    _stream_info,
    assert_drained,
    make_nats_queue,
    use_telegram,
)

HOUR = 3600.0
TOKEN = "share" + "Token" + "Marker"
SHARE_DROPPED = "Notification dropped: {} already holds {} rate-limited notifications, its share of the queue"
LIFETIME_DROPPED = "Notification dropped after 24 hours of rate limiting (discord)"
BACKENDS = pytest.mark.parametrize("backend", ["memory", "nats"])


@pytest.fixture
def shifted_clock(monkeypatch):
    state = SimpleNamespace(offset=0.0)
    state.monotonic = lambda: time.monotonic() + state.offset
    monkeypatch.setattr(notification_client, "time", SimpleNamespace(time=lambda: time.time() + state.offset))
    monkeypatch.setattr(nats_queue, "time", SimpleNamespace(monotonic=state.monotonic))
    monkeypatch.setattr(notification_client, "_rate_limits", {})
    return state


def _use_queue(backend, clock, monkeypatch, size):
    if backend == "memory":
        queue = InMemoryNotificationQueue(maxsize=size)
    else:
        queue = make_nats_queue(clock, max_msgs=size)
        queue.maxsize = size
    monkeypatch.setattr(queue_manager, "queue_instance", queue)
    return queue


class _Destinations:
    def __init__(self):
        self.limited: set[str] = set()
        self.failures: dict[str, int] = {}
        self.stalled: set[str] = set()
        self.answer = asyncio.Event()
        self.seen: list[tuple[str, str, int]] = []

    async def handle(self, request):
        webhook = request.match_info["webhook"]
        content = (await request.json())["content"]
        status = 429 if webhook in self.limited else 204
        if self.failures.get(content):
            self.failures[content] -= 1
            status = 500
        if content in self.stalled:
            await self.answer.wait()
        self.seen.append((webhook, content, status))
        if status == 500:
            return web.Response(status=500)
        if status == 429:
            return web.json_response({"retry_after": HOUR}, status=429)
        return web.Response(status=204)

    def contents(self, webhook, status):
        return [content for seen, content, answered in self.seen if (seen, answered) == (webhook, status)]


@contextlib.asynccontextmanager
async def _real_client_against(destinations, monkeypatch):
    application = web.Application()
    application.router.add_post("/api/webhooks/{webhook}/{token}", destinations.handle)
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
    base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/api/webhooks"
    try:
        yield lambda webhook: f"{base}/{webhook}/{TOKEN}"
    finally:
        await notification_client.client.close()
        await runner.cleanup()


async def _due(queue, steps: int = 200) -> None:
    for _ in range(steps):
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
async def test_a_destination_that_never_stops_answering_429_cannot_fill_the_queue_for_the_others(
    backend, shifted_clock, monkeypatch, caplog
):
    queue = _use_queue(backend, shifted_clock, monkeypatch, size=50)
    destinations = _Destinations()
    destinations.limited.add("111")
    caplog.set_level(logging.WARNING)
    async with _real_client_against(destinations, monkeypatch) as url:
        for number in range(49):
            await queue_manager.enqueue_discord({"content": f"limited {number}"}, url("111"))
        await _due(queue)
        for number in range(10):
            await queue_manager.enqueue_discord({"content": f"healthy {number}"}, url("333"))
        await _due(queue)

    assert destinations.contents("333", 204) == [f"healthy {number}" for number in range(10)]
    assert _lines(caplog, "Notification queue is full") == []
    assert _lines(caplog, "Notification dropped:") == [SHARE_DROPPED.format("Discord webhook 111", 5)] * 44
    assert destinations.contents("111", 429) == ["limited 0"]
    assert _queued(queue) == 5
    assert TOKEN not in caplog.text


@BACKENDS
async def test_the_share_drop_counts_against_its_own_destination_only(backend, shifted_clock, monkeypatch, caplog):
    queue = _use_queue(backend, shifted_clock, monkeypatch, size=20)
    destinations = _Destinations()
    destinations.limited.update({"111", "222"})
    caplog.set_level(logging.WARNING)
    async with _real_client_against(destinations, monkeypatch) as url:
        for content, webhook in [
            ("a1", "111"),
            ("b1", "222"),
            ("a2", "111"),
            ("h1", "333"),
            ("a3", "111"),
            ("b2", "222"),
            ("a4", "111"),
            ("h2", "333"),
        ]:
            await queue_manager.enqueue_discord({"content": content}, url(webhook))
        await _due(queue)
        assert _lines(caplog, "Notification dropped:") == [SHARE_DROPPED.format("Discord webhook 111", 2)] * 2
        await queue_manager.enqueue_discord({"content": "b3"}, url("222"))
        await _due(queue)

    assert _lines(caplog, "Notification dropped:") == [SHARE_DROPPED.format("Discord webhook 111", 2)] * 2 + [
        SHARE_DROPPED.format("Discord webhook 222", 2)
    ]
    assert destinations.contents("333", 204) == ["h1", "h2"]
    assert (destinations.contents("111", 429), destinations.contents("222", 429)) == (["a1"], ["b1"])
    assert _queued(queue) == 4


async def test_a_telegram_chat_is_named_by_its_id_when_its_share_is_used_up(shifted_clock, monkeypatch, caplog):
    queue = _use_queue("memory", shifted_clock, monkeypatch, size=20)
    api = use_telegram(monkeypatch, shifted_clock, max_retries=3)
    api.floods[-100111] = [HOUR] * 5
    caplog.set_level(logging.WARNING)
    for number in range(3):
        await queue_manager.enqueue_telegram(f"m{number}", chat_id=-100111)
    await queue_manager.enqueue_telegram("other", chat_id=-100222)
    await _due(queue)

    assert _lines(caplog, "Notification dropped:") == [SHARE_DROPPED.format("Telegram chat -100111", 2)]
    assert api.texts(-100222) == ["other"]
    assert _queued(queue) == 2


@BACKENDS
async def test_the_day_long_bound_still_drops_held_messages_and_frees_their_share(
    backend, shifted_clock, monkeypatch, caplog
):
    queue = _use_queue(backend, shifted_clock, monkeypatch, size=20)
    destinations = _Destinations()
    destinations.limited.add("111")
    caplog.set_level(logging.WARNING)
    async with _real_client_against(destinations, monkeypatch) as url:
        for number in range(3):
            await queue_manager.enqueue_discord({"content": f"held {number}"}, url("111"))
        await _due(queue)
        await _hours(queue, shifted_clock, 24)
        assert _lines(caplog, "Notification dropped after") == [LIFETIME_DROPPED] * 2
        await queue_manager.enqueue_discord({"content": "after the day"}, url("111"))
        await _due(queue)
        assert _queued(queue) == 1
        destinations.limited.clear()
        await _hours(queue, shifted_clock, 2)

    assert _lines(caplog, "Notification dropped:") == [SHARE_DROPPED.format("Discord webhook 111", 2)]
    assert destinations.contents("111", 204) == ["after the day"]
    assert_drained(queue)


@BACKENDS
async def test_a_destination_that_answers_again_takes_new_messages_once_its_held_ones_are_delivered(
    backend, shifted_clock, monkeypatch, caplog
):
    queue = _use_queue(backend, shifted_clock, monkeypatch, size=20)
    destinations = _Destinations()
    destinations.limited.add("111")
    caplog.set_level(logging.WARNING)
    async with _real_client_against(destinations, monkeypatch) as url:
        for number in range(3):
            await queue_manager.enqueue_discord({"content": f"held {number}"}, url("111"))
        await _due(queue)
        destinations.limited.clear()
        await _hours(queue, shifted_clock, 1)
        await queue_manager.enqueue_discord({"content": "after recovery"}, url("111"))
        await _due(queue)
        await _hours(queue, shifted_clock, 1)

    assert _lines(caplog, "Notification dropped:") == [SHARE_DROPPED.format("Discord webhook 111", 2)]
    assert destinations.contents("111", 204) == ["held 0", "held 1", "after recovery"]
    assert_drained(queue)


@BACKENDS
async def test_a_held_message_that_leaves_unsent_stops_counting_a_minute_after_it_was_due_back(
    backend, shifted_clock, monkeypatch, caplog
):
    queue = _use_queue(backend, shifted_clock, monkeypatch, size=20)
    destinations = _Destinations()
    destinations.limited.add("111")
    destinations.failures["failed twice"] = 2
    caplog.set_level(logging.WARNING)
    async with _real_client_against(destinations, monkeypatch) as url:
        await queue_manager.enqueue_discord({"content": "failed twice"}, url("111"))
        await _due(queue)
        shifted_clock.offset += 2
        await _due(queue)
        await queue_manager.enqueue_discord({"content": "keeps the schedule"}, url("111"))
        await _due(queue)
        shifted_clock.offset += 2
        await _due(queue)
        lowered = NotificationSettings.model_validate(
            {
                "notify_discord": True,
                "discord_webhook_url": "https://discord.com/api/webhooks/1/probe",
                "max_retries": 2,
            }
        )

        async def lowered_settings():
            return lowered

        monkeypatch.setattr(notification_client, "notification_settings", lowered_settings)
        shifted_clock.offset += HOUR + 1
        await _due(queue)
        await queue_manager.enqueue_discord({"content": "refused"}, url("111"))
        await _due(queue)
        shifted_clock.offset += 120
        await queue_manager.enqueue_discord({"content": "admitted"}, url("111"))
        await _due(queue)

    assert destinations.contents("111", 500) == ["failed twice"] * 2
    assert destinations.contents("111", 429) == ["keeps the schedule"] * 2
    assert _lines(caplog, "Notification dropped:") == [SHARE_DROPPED.format("Discord webhook 111", 2)]
    assert _queued(queue) == 2


@BACKENDS
async def test_a_message_arriving_while_its_destination_answers_is_counted_after_the_answer(
    backend, shifted_clock, monkeypatch, caplog
):
    queue = _use_queue(backend, shifted_clock, monkeypatch, size=20)
    destinations = _Destinations()
    destinations.limited.add("111")
    caplog.set_level(logging.WARNING)
    async with _real_client_against(destinations, monkeypatch) as url:
        await queue_manager.enqueue_discord({"content": "front"}, url("111"))
        await queue_manager.enqueue_discord({"content": "behind"}, url("111"))
        await _due(queue)
        destinations.limited.clear()
        destinations.stalled.add("front")
        shifted_clock.offset += HOUR + 0.5
        front = await queue.dequeue(timeout=0)
        answering = asyncio.create_task(notification_client.process_notification(front))
        await queue_manager.enqueue_discord({"content": "arrived meanwhile"}, url("111"))
        arrived = await queue.dequeue(timeout=0)
        waiting = asyncio.create_task(notification_client.process_notification(arrived))
        await asyncio.sleep(0.2)
        both_waiting = (answering.done(), waiting.done())
        destinations.answer.set()
        await asyncio.gather(answering, waiting)

    assert both_waiting == (False, False)
    assert destinations.contents("111", 204) == ["front"]
    assert _lines(caplog, "Notification dropped:") == []
    assert _queued(queue) == 2


@pytest.mark.parametrize(("existing", "expected"), [(None, QUEUE_MAX_MESSAGES), (200, 200)])
async def test_the_dispatcher_queue_knows_the_message_limit_of_its_stream(existing, expected):
    infos = {}
    if existing is not None:
        infos[STREAM] = _stream_info(
            StreamConfig(name=STREAM, subjects=[SUBJECT], retention=RetentionPolicy.WORK_QUEUE, max_msgs=existing)
        )
    queue = NatsNotificationQueue(stream_name=STREAM, subject=SUBJECT, consumer_name="workers")
    queue._js = _startup_js(infos)

    await queue._initialize_stream(create_consumer=False)

    assert queue.maxsize == expected
