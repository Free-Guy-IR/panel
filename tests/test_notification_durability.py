import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from types import SimpleNamespace

import pytest

from app.fork import notification_spool
from app.jobs import process_notification_queues, send_notifications as send_module
from app.notification import client as client_module, queue_manager
from app.notification.nats_queue import InMemoryNotificationQueue, NatsNotificationQueue


class _Msg:
    def __init__(self, consumer, item: dict):
        self.consumer = consumer
        self.data = json.dumps(item).encode()
        self.acks = 0
        self.naks = 0
        self.terms = 0

    async def ack(self):
        self.acks += 1

    async def nak(self):
        self.naks += 1
        self.consumer.pending.append(self)

    async def term(self):
        self.terms += 1

    async def in_progress(self):
        return None


class _Consumer:
    def __init__(self):
        self.pending: deque[_Msg] = deque()

    async def fetch(self, batch, timeout):
        if not self.pending:
            await asyncio.sleep(timeout)
            raise TimeoutError
        return [self.pending.popleft()]


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _telegram(message: str) -> dict:
    return queue_manager.TelegramNotification(message=message, chat_id=1).model_dump()


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
async def test_in_memory_release_puts_items_back_at_the_front_and_drain_returns_the_whole_backlog_in_order():
    queue = InMemoryNotificationQueue()
    for index in range(4):
        await queue.enqueue({"n": index})
    first = await queue.dequeue(timeout=0.1)
    second = await queue.dequeue(timeout=0.1)

    await queue.release(first, second)

    assert await queue.dequeue(timeout=0.1) == {"n": 0}
    await queue.release({"n": 0})
    assert queue.drain_nowait() == [{"n": 0}, {"n": 1}, {"n": 2}, {"n": 3}]
    assert queue.drain_nowait() == []


@pytest.mark.asyncio
async def test_nats_redelivers_a_cancelled_in_flight_item_exactly_once_and_never_spills_it(
    monkeypatch: pytest.MonkeyPatch, spool_path
):
    consumer = _Consumer()
    queue = NatsNotificationQueue()
    queue._consumer = consumer
    item = _telegram("in flight")
    msg = _Msg(consumer, item)
    consumer.pending.append(msg)
    delivered = []
    stuck = asyncio.Event()

    async def process(value):
        if not delivered and not stuck.is_set():
            stuck.set()
            await asyncio.Event().wait()
        delivered.append(value["message"])

    monkeypatch.setattr(client_module, "process_notification", process)
    monkeypatch.setattr(queue_manager, "queue_instance", queue)
    monkeypatch.setattr(queue_manager, "webhook_queue_instance", NatsNotificationQueue())

    in_flight = await queue.dequeue(timeout=0.1)
    task = asyncio.create_task(client_module.process_queued_notification(queue, in_flight))
    await asyncio.wait_for(stuck.wait(), timeout=1.0)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert (msg.acks, msg.naks) == (0, 1)
    assert queue.drain_nowait() == []
    await notification_spool.persist_notification_backlog()
    assert not spool_path.exists()

    redelivered = await queue.dequeue(timeout=0.1)
    await client_module.process_queued_notification(queue, redelivered)

    assert delivered == ["in flight"]
    assert (msg.acks, msg.naks) == (1, 1)
    assert await queue.dequeue(timeout=0.05) is None


def _webhook_settings() -> SimpleNamespace:
    return SimpleNamespace(
        enable=True,
        proxy_url=None,
        recurrent=3,
        timeout=0.3,
        webhooks=[SimpleNamespace(url="https://hooks.invalid/x", secret=None)],
    )


def _patch_webhook_sending(monkeypatch: pytest.MonkeyPatch, sent: list, *, block: asyncio.Event | None = None):
    async def settings():
        return _webhook_settings()

    async def send_to_all(client, payloads, webhooks):
        if block is not None:
            await block.wait()
        sent.extend(payload["event"] for payload in payloads)
        return True

    monkeypatch.setattr(send_module, "webhook_settings", settings)
    monkeypatch.setattr(send_module, "send_to_all_webhooks", send_to_all)
    monkeypatch.setattr(send_module, "create_outbound_http_session", lambda proxy=None: _Session())


@pytest.mark.asyncio
async def test_backlog_retries_and_an_in_flight_item_survive_a_restart_and_are_each_delivered_once(
    monkeypatch: pytest.MonkeyPatch, spool_path
):
    queue, _ = _fresh_queues(monkeypatch)
    delivered: list[str] = []
    sent: list[str] = []
    in_flight_started = asyncio.Event()
    blocked_once: set[str] = set()

    async def process(value):
        message = value["message"]
        if message == "in flight" and message not in blocked_once:
            blocked_once.add(message)
            in_flight_started.set()
            await asyncio.Event().wait()
        delivered.append(message)

    monkeypatch.setattr(client_module, "process_notification", process)
    _patch_webhook_sending(monkeypatch, sent)

    await queue.enqueue(_telegram("in flight"))
    await client_module.start_notification_dispatcher()
    await asyncio.wait_for(in_flight_started.wait(), timeout=2.0)
    for index in range(3):
        await queue.enqueue(_telegram(f"backlog {index}"))
    await queue_manager.enqueue_webhook({"event": "retry"}, send_at=time.time() + 0.3, tries=1)
    await send_module.send_notifications()
    assert sent == []

    await notification_spool.persist_notification_backlog()

    assert delivered == []
    saved = json.loads(spool_path.read_text())["notifications"]
    assert [entry["item"].get("message") or entry["item"]["payload"]["event"] for entry in saved] == [
        "in flight",
        "backlog 0",
        "backlog 1",
        "backlog 2",
        "retry",
    ]

    _fresh_queues(monkeypatch)
    await notification_spool.restore_notification_backlog()
    assert not spool_path.exists()

    await process_notification_queues.process_all_notification_queues()
    await asyncio.sleep(0.35)
    await send_module.send_notifications()

    assert delivered == ["in flight", "backlog 0", "backlog 1", "backlog 2"]
    assert sent == ["retry"]

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
async def test_a_webhook_batch_cancelled_mid_send_hands_every_item_back_once_in_order(
    monkeypatch: pytest.MonkeyPatch,
):
    _, webhooks = _fresh_queues(monkeypatch)
    sent: list[str] = []
    block = asyncio.Event()
    _patch_webhook_sending(monkeypatch, sent, block=block)
    for name in ("a", "b", "c"):
        await queue_manager.enqueue_webhook({"event": name}, send_at=time.time() - 1)

    task = asyncio.create_task(send_module.send_notifications())
    for _ in range(100):
        if webhooks.q.empty() and not webhooks._returned:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(1.1)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert sent == []
    assert [item["payload"]["event"] for item in webhooks.drain_nowait()] == ["a", "b", "c"]
