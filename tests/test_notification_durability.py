import asyncio
import contextlib
import json
import logging
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from aiohttp import web

from app.fork import notification_spool
from app.jobs import process_notification_queues, send_notifications as send_module
from app.models.settings import NotificationSettings, Webhook
from app.notification import client as client_module, nats_queue, queue_manager
from app.notification.nats_queue import InMemoryNotificationQueue
from tests.test_notification_delivery import make_nats_queue


class _Receiver:
    def __init__(self):
        self.delivered: list[str] = []
        self.webhook_events: list[str] = []
        self.stalled: list[str] = []
        self.stall_first: set[str] = set()
        self.release = asyncio.Event()

    async def notify(self, request):
        content = (await request.json())["content"]
        if content in self.stall_first:
            self.stall_first.discard(content)
            self.stalled.append(content)
            await self.release.wait()
            return web.Response(status=503)
        self.delivered.append(content)
        return web.Response(status=204)

    async def hook(self, request):
        self.webhook_events.extend(item["event"] for item in await request.json())
        return web.Response(status=204)


@contextlib.asynccontextmanager
async def _local_receiver():
    receiver = _Receiver()
    application = web.Application()
    application.router.add_post("/notify", receiver.notify)
    application.router.add_post("/hook", receiver.hook)
    runner = web.AppRunner(application)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    receiver.base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    try:
        yield receiver
    finally:
        receiver.release.set()
        await runner.cleanup()


async def _use_notification_receiver(monkeypatch: pytest.MonkeyPatch, receiver: _Receiver) -> None:
    stored = NotificationSettings.model_validate(
        {"notify_discord": True, "discord_webhook_url": "https://discord.com/api/webhooks/1/probe", "max_retries": 3}
    )

    async def settings():
        return stored

    monkeypatch.setattr(client_module, "notification_settings", settings)
    monkeypatch.setattr(client_module, "client", None)
    await client_module.define_client()


def _use_webhook_receiver(monkeypatch: pytest.MonkeyPatch, receiver: _Receiver) -> None:
    stored = Webhook(enable=True, webhooks=[{"url": f"{receiver.base}/hook", "secret": "s"}], timeout=1, recurrent=3)

    async def settings():
        return stored

    monkeypatch.setattr(send_module, "webhook_settings", settings)


async def _discord(receiver: _Receiver, content: str) -> None:
    await queue_manager.enqueue_discord({"content": content}, f"{receiver.base}/notify")


def _telegram(message: str) -> dict:
    return queue_manager.TelegramNotification(message=message, chat_id=1).model_dump()


def _shifted_clock() -> SimpleNamespace:
    state = SimpleNamespace(offset=0.0)
    state.monotonic = lambda: time.monotonic() + state.offset
    return state


async def _wait_for(condition, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise TimeoutError
        await asyncio.sleep(0.01)


@pytest.fixture
def shifted_clock(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = _shifted_clock()

    class ShiftedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(seconds=state.offset)

    monkeypatch.setattr(nats_queue, "time", SimpleNamespace(monotonic=state.monotonic))
    monkeypatch.setattr(send_module, "dt", ShiftedDatetime)
    monkeypatch.setattr(client_module, "time", SimpleNamespace(time=lambda: time.time() + state.offset))
    return state


@pytest.fixture
def spool_path(tmp_path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "notification_spool.json"
    monkeypatch.setattr(notification_spool, "SPOOL_PATH", path)
    return path


def _fresh_queues(monkeypatch: pytest.MonkeyPatch) -> tuple[InMemoryNotificationQueue, InMemoryNotificationQueue]:
    queue = InMemoryNotificationQueue()
    webhooks = InMemoryNotificationQueue()
    monkeypatch.setattr(queue_manager, "queue_instance", queue)
    monkeypatch.setattr(queue_manager, "webhook_queue_instance", webhooks)
    return queue, webhooks


@pytest.mark.asyncio
async def test_in_memory_deliveries_handed_back_are_drained_with_the_backlog_in_due_order():
    queue = InMemoryNotificationQueue()
    for index in range(4):
        await queue.enqueue({"n": index})
    first = await queue.dequeue(timeout=0.1)
    second = await queue.dequeue(timeout=0.1)

    await first.release(delay=0)
    await second.release(delay=0)

    again = await queue.dequeue(timeout=0.1)
    assert again.data == {"n": 2}
    await again.release(delay=0)
    assert [item["n"] for item in queue.drain_nowait()] == [3, 0, 1, 2]
    assert queue.drain_nowait() == []


@pytest.mark.asyncio
async def test_nats_redelivers_a_cancelled_in_flight_item_exactly_once_and_never_spills_it(
    monkeypatch: pytest.MonkeyPatch, spool_path
):
    clock = _shifted_clock()
    queue = make_nats_queue(clock)
    monkeypatch.setattr(queue_manager, "queue_instance", queue)
    monkeypatch.setattr(queue_manager, "webhook_queue_instance", make_nats_queue(clock))
    async with _local_receiver() as receiver:
        await _use_notification_receiver(monkeypatch, receiver)
        receiver.stall_first.add("in flight")
        await _discord(receiver, "in flight")

        in_flight = await queue.dequeue(timeout=0.1)
        task = asyncio.create_task(client_module.process_notification(in_flight))
        await _wait_for(lambda: receiver.stalled)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        message = queue._consumer.messages[0]
        assert (message.acked, message.naks) == (False, [1])
        assert queue.drain_nowait() == []
        await notification_spool.persist_notification_backlog()
        assert not spool_path.exists()

        clock.offset += 2
        redelivered = await queue.dequeue(timeout=0.1)
        await client_module.process_notification(redelivered)
        await client_module.client.close()

    assert receiver.delivered == ["in flight"]
    assert queue._consumer.messages[1].acked
    assert await queue.dequeue(timeout=0.05) is None


@pytest.mark.asyncio
async def test_backlog_retries_and_an_in_flight_item_survive_a_restart_and_are_each_delivered_once(
    monkeypatch: pytest.MonkeyPatch, spool_path, shifted_clock
):
    _fresh_queues(monkeypatch)
    async with _local_receiver() as receiver:
        await _use_notification_receiver(monkeypatch, receiver)
        _use_webhook_receiver(monkeypatch, receiver)
        receiver.stall_first.add("in flight")
        try:
            await _discord(receiver, "in flight")
            await client_module.start_notification_dispatcher()
            await _wait_for(lambda: receiver.stalled)
            for index in range(3):
                await _discord(receiver, f"backlog {index}")
            await queue_manager.enqueue_webhook({"event": "retry"}, send_at=time.time() + 30, tries=1)
            await send_module.send_notifications()
            assert receiver.webhook_events == []

            await notification_spool.persist_notification_backlog()

            assert receiver.delivered == []
            saved = json.loads(spool_path.read_text())["notifications"]
            assert [
                entry["item"].get("json_data", {}).get("content") or entry["item"]["payload"]["event"]
                for entry in saved
            ] == ["backlog 0", "backlog 1", "backlog 2", "in flight", "retry"]

            _fresh_queues(monkeypatch)
            await notification_spool.restore_notification_backlog()
            assert not spool_path.exists()

            await process_notification_queues.process_all_notification_queues()
            shifted_clock.offset += 31
            await send_module.send_notifications()
        finally:
            await client_module.stop_notification_dispatcher()
            if client_module.client is not None:
                await client_module.client.close()

    assert receiver.delivered == ["backlog 0", "backlog 1", "backlog 2", "in flight"]
    assert receiver.webhook_events == ["retry"]

    second_queue, second_webhooks = _fresh_queues(monkeypatch)
    await notification_spool.restore_notification_backlog()
    assert second_queue.drain_nowait() == []
    assert second_webhooks.drain_nowait() == []


@pytest.mark.asyncio
async def test_a_partial_spool_file_is_quarantined_loudly_and_nothing_is_guessed(
    monkeypatch: pytest.MonkeyPatch, spool_path, caplog
):
    queue, webhooks = _fresh_queues(monkeypatch)
    partial = b'{"version": 1, "notifications": [{"queue": "notifications", "item": {"type": "tele'
    spool_path.write_bytes(partial)

    with caplog.at_level(logging.ERROR):
        await notification_spool.restore_notification_backlog()

    quarantined = list(spool_path.parent.glob(f"{spool_path.name}.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == partial
    assert not spool_path.exists()
    assert "is unusable" in caplog.text
    assert queue.drain_nowait() == [] and webhooks.drain_nowait() == []


@pytest.mark.asyncio
async def test_unreadable_entries_are_kept_aside_loudly_while_the_readable_ones_are_requeued(
    monkeypatch: pytest.MonkeyPatch, spool_path, caplog
):
    queue, _ = _fresh_queues(monkeypatch)
    good = _telegram("good")
    spool_path.write_text(
        json.dumps(
            {
                "version": 1,
                "notifications": [
                    {"queue": "notifications", "item": good},
                    {"queue": "carrier-pigeon", "item": {"x": 1}},
                    "garbage",
                ],
            }
        )
    )

    with caplog.at_level(logging.ERROR):
        await notification_spool.restore_notification_backlog()

    assert queue.drain_nowait() == [good]
    kept = list(spool_path.parent.glob(f"{spool_path.name}.corrupt-*"))
    assert len(kept) == 1
    assert json.loads(kept[0].read_text())["notifications"] == [
        {"queue": "carrier-pigeon", "item": {"x": 1}},
        "garbage",
    ]
    assert "2 unreadable entries" in caplog.text
    assert not spool_path.exists()


@pytest.mark.asyncio
async def test_a_webhook_batch_cancelled_mid_send_hands_every_item_back_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
):
    _, webhooks = _fresh_queues(monkeypatch)
    stall = asyncio.Event()
    seen: list[list[str]] = []

    async def stalling_hook(request):
        seen.append([item["event"] for item in await request.json()])
        await stall.wait()
        return web.Response(status=204)

    application = web.Application()
    application.router.add_post("/hook", stalling_hook)
    runner = web.AppRunner(application)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/hook"
    stored = Webhook(enable=True, webhooks=[{"url": url, "secret": "s"}], timeout=1, recurrent=3)

    async def settings():
        return stored

    monkeypatch.setattr(send_module, "webhook_settings", settings)
    try:
        for name in ("a", "b", "c"):
            await queue_manager.enqueue_webhook({"event": name}, send_at=time.time() - 1)

        task = asyncio.create_task(send_module.send_notifications())
        await _wait_for(lambda: seen)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    finally:
        stall.set()
        await runner.cleanup()

    assert seen == [["a", "b", "c"]]
    assert sorted(item["payload"]["event"] for item in webhooks.drain_nowait()) == ["a", "b", "c"]
    assert webhooks.drain_nowait() == []
