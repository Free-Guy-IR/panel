import asyncio
import json
import os
import shutil
import socket
import subprocess
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import nats
import pytest
from nats.js.api import DiscardPolicy, RetentionPolicy, StorageType, StreamConfig
from nats.js.errors import NotFoundError

from app.notification.nats_queue import QUEUE_MAX_MESSAGES, NatsNotificationQueue
from app.notification.queue_manager import shutdown_queue
from config import nats_settings

OLD = 150
NEW = 150
COMPLETED = 10


class LostAck(Exception):
    pass


async def _lose_ack(*_args, **_kwargs):
    raise LostAck


async def _wait_for_port(port: int, process: subprocess.Popen):
    async with asyncio.timeout(10):
        while True:
            try:
                _reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                await writer.wait_closed()
                return
            except OSError:
                if process.poll() is not None:
                    raise RuntimeError("Test NATS server exited during startup")
                await asyncio.sleep(0.02)


@asynccontextmanager
async def jetstream(tmp_path, monkeypatch):
    binary = os.environ.get("NATS_SERVER_BINARY") or shutil.which("nats-server")
    process = None
    if binary:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        process = await asyncio.to_thread(
            subprocess.Popen,
            [binary, "-js", "-a", "127.0.0.1", "-p", str(port), "-sd", str(tmp_path / "nats")],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        url = f"nats://127.0.0.1:{port}"
    elif os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("NATS_URL"):
        url = os.environ["NATS_URL"]
    else:
        pytest.skip("Set NATS_SERVER_BINARY to run the JetStream retry state tests")
    nc = None
    token = uuid4().hex[:12]
    names = SimpleNamespace(
        stream=f"NOTIFY_{token}", subject=f"notify.{token}.queue", consumer=f"workers_{token}", queues=[]
    )
    try:
        if process is not None:
            await _wait_for_port(port, process)
        monkeypatch.setattr(nats_settings, "enabled", True)
        monkeypatch.setattr(nats_settings, "url", url)
        nc = await nats.connect(url)
        names.js = nc.jetstream()
        yield names
    finally:
        for queue in names.queues:
            await shutdown_queue(queue)
        if nc is not None:
            for stream in (names.stream, f"{names.stream}_RETRY_STATE"):
                try:
                    await names.js.delete_stream(stream)
                except NotFoundError:
                    pass
            await nc.close()
        if process is not None:
            process.terminate()
            await asyncio.to_thread(process.wait, timeout=10)


async def _start(names, create_consumer: bool) -> NatsNotificationQueue:
    queue = NatsNotificationQueue(stream_name=names.stream, subject=names.subject, consumer_name=names.consumer)
    names.queues.append(queue)
    await queue.initialize(create_consumer=create_consumer)
    return queue


async def _stop(names, queue: NatsNotificationQueue):
    names.queues.remove(queue)
    await shutdown_queue(queue)


async def _publish(names, queue: NatsNotificationQueue, item: dict, legacy: bool):
    if legacy:
        await names.js.publish(names.subject, json.dumps(item).encode())
    else:
        await queue.enqueue(item)


async def _messages(names, stream: str) -> int:
    return (await names.js.stream_info(stream)).state.messages


async def _use_storage(names, storage: str):
    if storage == "memory":
        await names.js.add_stream(
            StreamConfig(
                name=names.stream,
                subjects=[names.subject],
                retention=RetentionPolicy.WORK_QUEUE,
                discard=DiscardPolicy.NEW,
                max_msgs=QUEUE_MAX_MESSAGES,
                max_bytes=8 * 1024 * 1024,
                storage=StorageType.MEMORY,
            )
        )


async def _hold_retry_state(names, legacy: bool, count: int):
    old = await _start(names, create_consumer=True)
    for n in range(count):
        await _publish(names, old, {"kind": "old", "n": n}, legacy)
    for n in range(count):
        delivery = await old.dequeue(timeout=5)
        assert delivery.data == {"kind": "old", "n": n}
        async with delivery:
            if n >= count - COMPLETED:
                await delivery.prepare({"kind": "old", "n": n, "tries": 1})
                delivery.message.ack_sync = _lose_ack
                with pytest.raises(LostAck):
                    await delivery.ack()
                await delivery.release(3600)
            else:
                await delivery.retry({"kind": "old", "n": n, "tries": 1}, delay=3600)
    assert await _messages(names, f"{names.stream}_RETRY_STATE") == count
    await _stop(names, old)
    await names.js.delete_stream(names.stream)


async def _restart(names, legacy: bool, order: str) -> NatsNotificationQueue:
    if order == "scheduler-first":
        scheduler = await _start(names, create_consumer=True)
        for n in range(NEW):
            await _publish(names, scheduler, {"kind": "new", "n": n}, legacy)
        return scheduler
    producer = await _start(names, create_consumer=False)
    for n in range(NEW):
        await _publish(names, producer, {"kind": "new", "n": n}, legacy)
    return await _start(names, create_consumer=True)


async def _drain(names, queue: NatsNotificationQueue) -> list[dict]:
    delivered = []
    for _ in range(NEW * 2):
        delivery = await queue.dequeue(timeout=1)
        if delivery is None:
            if await _messages(names, names.stream) == 0:
                break
            continue
        async with delivery:
            delivered.append(delivery.data)
            await delivery.ack()
    return delivered


@pytest.mark.parametrize("legacy", [False, True], ids=["message-id", "no-message-id"])
@pytest.mark.parametrize("order", ["scheduler-first", "producer-first"])
async def test_retry_state_of_a_deleted_stream_never_replaces_a_new_message(tmp_path, monkeypatch, order, legacy):
    async with jetstream(tmp_path, monkeypatch) as names:
        await _hold_retry_state(names, legacy, OLD)
        scheduler = await _restart(names, legacy, order)

        delivered = await _drain(names, scheduler)

        assert [item for item in delivered if item["kind"] == "old"] == []
        assert delivered == [{"kind": "new", "n": n} for n in range(NEW)]
        assert await _messages(names, names.stream) == 0
        assert await _messages(names, f"{names.stream}_RETRY_STATE") == 0


async def test_startup_reclaims_retry_state_whose_sequence_now_holds_another_message(tmp_path, monkeypatch):
    async with jetstream(tmp_path, monkeypatch) as names:
        await _hold_retry_state(names, False, 30)
        producer = await _start(names, create_consumer=False)
        for n in range(30):
            await producer.enqueue({"kind": "new", "n": n})

        await _start(names, create_consumer=True)

        assert await _messages(names, f"{names.stream}_RETRY_STATE") == 0
        assert await _messages(names, names.stream) == 30


async def test_retry_state_without_a_message_identity_is_not_trusted(tmp_path, monkeypatch):
    async with jetstream(tmp_path, monkeypatch) as names:
        queue = await _start(names, create_consumer=True)
        await queue.enqueue({"kind": "own"})
        await names.js.publish(
            f"{queue.RETRY_SUBJECT}.1", json.dumps({"data": {"kind": "other"}, "completed": True}).encode()
        )

        delivery = await queue.dequeue(timeout=5)

        assert delivery is not None
        assert delivery.data == {"kind": "own"}
        assert await _messages(names, f"{names.stream}_RETRY_STATE") == 0
        async with delivery:
            await delivery.ack()
        assert await _messages(names, names.stream) == 0


@pytest.mark.parametrize("legacy", [False, True], ids=["message-id", "no-message-id"])
async def test_retry_state_follows_its_own_message_across_redeliveries(tmp_path, monkeypatch, legacy):
    async with jetstream(tmp_path, monkeypatch) as names:
        queue = await _start(names, create_consumer=True)
        await _publish(names, queue, {"kind": "own", "tries": 0}, legacy)

        first = await queue.dequeue(timeout=5)
        async with first:
            await first.prepare({"kind": "own", "tries": 0})
            await first.retry({"kind": "own", "tries": 1}, delay=0.05)
        second = await queue.dequeue(timeout=5)
        assert second.data == {"kind": "own", "tries": 1}
        async with second:
            await second.retry({"kind": "own", "tries": 2}, delay=0.05)
        third = await queue.dequeue(timeout=5)
        assert third.data == {"kind": "own", "tries": 2}
        async with third:
            third.message.ack_sync = _lose_ack
            with pytest.raises(LostAck):
                await third.ack()
            await third.release(0.05)

        assert await queue.dequeue(timeout=5) is None
        assert await _messages(names, names.stream) == 0
        assert await _messages(names, f"{names.stream}_RETRY_STATE") == 0


class Interleaved:
    def __init__(self, js, stream: str, hook):
        self._js = js
        self._stream = stream
        self._hook = hook

    def __getattr__(self, name):
        return getattr(self._js, name)

    async def get_msg(self, stream_name, *args, **kwargs):
        result = await self._js.get_msg(stream_name, *args, **kwargs)
        if stream_name == self._stream and self._hook is not None:
            hook, self._hook = self._hook, None
            await hook()
        return result


@pytest.mark.parametrize("storage", ["file", "memory"])
async def test_an_ack_that_removes_the_retry_state_under_a_running_prune_is_not_an_error(
    tmp_path, monkeypatch, storage
):
    async with jetstream(tmp_path, monkeypatch) as names:
        await _use_storage(names, storage)
        consumer = await _start(names, create_consumer=True)
        pruner = await _start(names, create_consumer=False)
        await consumer.enqueue({"kind": "own"})
        delivery = await consumer.dequeue(timeout=5)
        await delivery.prepare({"kind": "own"})

        async def ack_in_between():
            await delivery.ack()

        pruner._js = Interleaved(pruner._js, pruner.RETRY_STREAM, ack_in_between)
        await pruner._prune_retry_state()

        assert pruner._js._hook is None
        assert delivery.settled
        assert (await names.js.stream_info(pruner.RETRY_STREAM)).config.storage == StorageType(storage)
        assert await _messages(names, names.stream) == 0
        assert await _messages(names, f"{names.stream}_RETRY_STATE") == 0


@pytest.mark.parametrize("storage", ["file", "memory"])
async def test_a_prune_that_removes_stale_retry_state_under_a_dequeue_still_delivers_the_new_message(
    tmp_path, monkeypatch, storage
):
    async with jetstream(tmp_path, monkeypatch) as names:
        await _use_storage(names, storage)
        await _hold_retry_state(names, False, OLD)
        scheduler = await _restart(names, False, "producer-first")
        pruner = await _start(names, create_consumer=False)

        async def prune_in_between():
            await pruner._prune_retry_state()

        scheduler._js = Interleaved(scheduler._js, scheduler.RETRY_STREAM, prune_in_between)
        delivered = await _drain(names, scheduler)

        assert scheduler._js._hook is None
        assert (await names.js.stream_info(scheduler.RETRY_STREAM)).config.storage == StorageType(storage)
        assert delivered == [{"kind": "new", "n": n} for n in range(NEW)]
        assert await _messages(names, f"{names.stream}_RETRY_STATE") == 0
