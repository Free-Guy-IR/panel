import asyncio
import contextlib
from uuid import uuid4

import nats
import pytest

from app.nats import kv_index
from app.nats.kv_watch import KvWatcher, watch_kv
from app.node.nats_memory import NatsUserSyncStore
from tests.test_nats_sync_integration import jetstream, wait_for_keys  # noqa: F401


class DroppingProxy:
    def __init__(self, port):
        self.port = port
        self.dropping = False
        self.dropped_data = asyncio.Event()
        self.reply_trigger = None
        self.request_seen = asyncio.Event()
        self.reply_lost = asyncio.Event()
        self.client_writers = []
        self.server = None

    async def start(self):
        self.server = await asyncio.start_server(self._link, "127.0.0.1", 0)
        return f"nats://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"

    async def _link(self, client_reader, client_writer):
        server_reader, server_writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.client_writers.append(client_writer)

        async def pump(reader, writer, from_server):
            with contextlib.suppress(Exception):
                while data := await reader.read(65536):
                    if not from_server and self.reply_trigger is not None and self.reply_trigger in data:
                        self.reply_trigger = None
                        self.dropping = True
                        self.request_seen.set()
                    if from_server and self.dropping:
                        if b"$JS.ACK." in data:
                            self.dropped_data.set()
                        if b"consumer_create_response" in data:
                            self.reply_lost.set()
                        continue
                    writer.write(data)
                    await writer.drain()
            writer.close()

        await asyncio.gather(
            pump(client_reader, server_writer, False),
            pump(server_reader, client_writer, True),
            return_exceptions=True,
        )

    async def drop_next_delivery(self, write):
        self.dropped_data.clear()
        self.dropping = True
        revision = await write()
        async with asyncio.timeout(10):
            await self.dropped_data.wait()
        return revision

    def drop_reply_to(self, request):
        self.request_seen.clear()
        self.reply_lost.clear()
        self.reply_trigger = request

    def cut_clients(self):
        for writer in self.client_writers:
            writer.transport.abort()
        self.client_writers.clear()
        self.dropping = False

    async def close(self):
        self.cut_clients()
        self.server.close()


async def wait_for_reconnect(nc, count=1):
    async with asyncio.timeout(10):
        while nc.stats["reconnects"] < count:
            await asyncio.sleep(0.02)


async def wait_for_exact_keys(store, prefix, expected, timeout=30):
    async with asyncio.timeout(timeout):
        while set(await store._key_index.keys(prefix)) != expected:
            await asyncio.sleep(0.01)


async def consumer_count(js, bucket):
    return (await js.stream_info(f"KV_{bucket}")).state.consumer_count


async def test_a_live_update_lost_on_a_dead_connection_still_reaches_the_index(jetstream):  # noqa: F811
    port = int(jetstream.url.rsplit(":", 1)[1])
    proxy = DroppingProxy(port)
    nc = await nats.connect(await proxy.start(), reconnect_time_wait=0.05)
    store = NatsUserSyncStore(await nc.jetstream().key_value(jetstream.bucket))
    try:
        assert await store.claim_users("1", "reader", 10, 30) == []
        await proxy.drop_next_delivery(lambda: jetstream.kv.put("p.1.lost", b'{"email":"lost","user":"CgRsb3N0"}'))
        proxy.cut_clients()
        await wait_for_reconnect(nc)
        await wait_for_keys(store, "p.1.", 1, timeout=20)
        assert len(nc._subs) <= 2
    finally:
        await store.close()
        with contextlib.suppress(Exception):
            await nc.close()
        await proxy.close()


@pytest.mark.parametrize("ending", ["connection-dies", "connection-survives"])
async def test_a_lost_resubscribe_reply_rebuilds_the_index_without_losing_a_key(jetstream, monkeypatch, ending):  # noqa: F811
    inactive_threshold = 3
    builds = []

    async def counted_watch_kv(kv, pattern, **kwargs):
        builds.append(pattern)
        return await watch_kv(kv, pattern, **{**kwargs, "inactive_threshold": inactive_threshold})

    monkeypatch.setattr(kv_index, "watch_kv", counted_watch_kv)
    payload = b'{"email":"kept","user":"CgRrZXB0"}'
    port = int(jetstream.url.rsplit(":", 1)[1])
    proxy = DroppingProxy(port)
    nc = await nats.connect(await proxy.start(), reconnect_time_wait=0.05)
    store = NatsUserSyncStore(await nc.jetstream().key_value(jetstream.bucket))
    try:
        await jetstream.kv.put("p.1.before", payload)
        await wait_for_exact_keys(store, "p.1.", {"p.1.before"})
        assert len(builds) == 1
        proxy.drop_reply_to(b"$JS.API.CONSUMER.CREATE")
        proxy.cut_clients()
        async with asyncio.timeout(10):
            await (proxy.request_seen if ending == "connection-dies" else proxy.reply_lost).wait()
        if ending == "connection-survives":
            proxy.dropping = False
        await jetstream.kv.put("p.1.during", payload)
        await jetstream.kv.delete("p.1.before")
        if ending == "connection-dies":
            proxy.cut_clients()
            await wait_for_reconnect(nc, 2)
        await wait_for_exact_keys(store, "p.1.", {"p.1.during"})
        assert len(builds) >= 2
        assert await consumer_count(jetstream.js, jetstream.bucket) <= 2
        async with asyncio.timeout(inactive_threshold * 4):
            while await consumer_count(jetstream.js, jetstream.bucket) != 1:
                await asyncio.sleep(0.1)
        await jetstream.kv.put("p.1.after", payload)
        await wait_for_exact_keys(store, "p.1.", {"p.1.during", "p.1.after"})
        assert len(nc._subs) <= 2
    finally:
        await store.close()
        with contextlib.suppress(Exception):
            await nc.close()
        await proxy.close()


class Reader:
    def __init__(self, watcher):
        self.watcher = watcher
        self.revisions = []
        self.ready = asyncio.Event()
        self.gate = asyncio.Event()
        self.gate.set()
        self.task = asyncio.create_task(self._run())

    async def _run(self):
        async for entry in self.watcher:
            if entry is None:
                self.ready.set()
            elif self.ready.is_set():
                self.revisions.append(entry.revision)
                await self.gate.wait()

    async def reached(self, revision, timeout=20):
        async with asyncio.timeout(timeout):
            while not self.revisions or self.revisions[-1] < revision:
                if self.task.done():
                    self.task.result()
                    raise AssertionError("the watcher stopped")
                await asyncio.sleep(0.01)

    async def close(self):
        self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.task
        await self.watcher.stop()


async def last_revision(js, bucket):
    return (await js.stream_info(f"KV_{bucket}")).state.last_seq


@pytest.mark.parametrize(
    "interleaving",
    ["quiet-after-reconnect", "writes-across-reconnect", "gap-without-reconnect", "blocked-reader-dedup"],
)
async def test_a_dying_connection_loses_and_replays_no_revision(jetstream, monkeypatch, interleaving):  # noqa: F811
    bucket = "watch_" + uuid4().hex
    writer = await jetstream.js.create_key_value(bucket=bucket, history=64)
    for number in range(5):
        await writer.put(f"p.1.seed{number}", b"seed")
    await writer.delete("p.1.seed0")
    if interleaving == "blocked-reader-dedup":
        monkeypatch.setattr(KvWatcher, "BATCH_SIZE", 2)
    port = int(jetstream.url.rsplit(":", 1)[1])
    proxy = DroppingProxy(port)
    nc = await nats.connect(await proxy.start(), reconnect_time_wait=0.05)
    watermark = await last_revision(jetstream.js, bucket)
    reader = Reader(await watch_kv(await nc.jetstream().key_value(bucket), ">"))
    try:
        async with asyncio.timeout(10):
            await reader.ready.wait()

        async def write(number):
            if number % 4 == 3:
                await writer.delete(f"p.1.key{number % 5}")
                return await last_revision(jetstream.js, bucket)
            return await writer.put(f"p.1.key{number % 5}", str(number).encode())

        await proxy.drop_next_delivery(lambda: write(0))
        if interleaving == "quiet-after-reconnect":
            proxy.cut_clients()
            await wait_for_reconnect(nc)
        elif interleaving == "writes-across-reconnect":
            for number in range(1, 10):
                await write(number)
            lost = await last_revision(jetstream.js, bucket)
            proxy.cut_clients()
            await wait_for_reconnect(nc)
            await reader.reached(lost)
            reader.gate.clear()
            await reader.reached(await write(10))
            acked = []
            enough_acked = asyncio.Event()

            async def stream(numbers):
                for number in numbers:
                    acked.append(await write(number))
                    if len(acked) >= 6:
                        enough_acked.set()

            writes = asyncio.gather(*(stream(range(start, 40, 3)) for start in (11, 12, 13)))
            async with asyncio.timeout(10):
                await enough_acked.wait()
            in_flight = 29 - len(acked)
            proxy.cut_clients()
            await wait_for_reconnect(nc, 2)
            reader.gate.set()
            await writes
            assert 0 < in_flight < 29
        elif interleaving == "gap-without-reconnect":
            proxy.dropping = False
            await write(1)
        else:
            reader.gate.clear()
            proxy.dropping = False
            for number in range(1, 20):
                await write(number)
            await reader.reached(watermark + 1)
            proxy.dropping = True
            for number in range(20, 25):
                await write(number)
            proxy.cut_clients()
            await wait_for_reconnect(nc)
            reader.gate.set()
        final = await last_revision(jetstream.js, bucket)
        await reader.reached(final)
        sentinel = await writer.put("p.1.sentinel", b"after")
        await reader.reached(sentinel)
        assert reader.revisions == [*range(watermark + 1, final + 1), sentinel]
        assert len(nc._subs) <= 2
    finally:
        await reader.close()
        with contextlib.suppress(Exception):
            await nc.close()
        await proxy.close()
        await jetstream.js.delete_key_value(bucket)


def drop_once_as_slow_consumer(subscription, revision):
    queue = subscription._sub._pending_queue
    put_nowait = queue.put_nowait
    dropped = asyncio.Event()

    def put_or_drop(msg):
        if not dropped.is_set() and msg.metadata.sequence.stream == revision:
            dropped.set()
            raise asyncio.QueueFull
        put_nowait(msg)

    queue.put_nowait = put_or_drop
    return dropped


@pytest.mark.parametrize("writers", ["sequential", "concurrent"])
async def test_a_slow_consumer_drop_loses_no_revision(jetstream, writers):  # noqa: F811
    bucket = "watch_" + uuid4().hex
    writer = await jetstream.js.create_key_value(bucket=bucket, history=64)
    await writer.put("p.1.seed", b"seed")
    nc = await nats.connect(jetstream.url)
    nc._error_cb = lambda error: asyncio.sleep(0)
    watermark = await last_revision(jetstream.js, bucket)
    reader = Reader(await watch_kv(await nc.jetstream().key_value(bucket), ">"))
    try:
        async with asyncio.timeout(10):
            await reader.ready.wait()
        dropped = drop_once_as_slow_consumer(reader.watcher._live, watermark + 3)

        async def stream(numbers):
            for number in numbers:
                await writer.put(f"p.1.key{number % 5}", str(number).encode())

        if writers == "sequential":
            await stream(range(10))
        else:
            await asyncio.gather(*(stream(range(start, 30, 3)) for start in (0, 1, 2)))
        async with asyncio.timeout(10):
            await dropped.wait()
        final = await last_revision(jetstream.js, bucket)
        await reader.reached(final)
        sentinel = await writer.put("p.1.sentinel", b"after")
        await reader.reached(sentinel)
        assert reader.revisions == [*range(watermark + 1, final + 1), sentinel]
        assert len(nc._subs) <= 2
    finally:
        await reader.close()
        with contextlib.suppress(Exception):
            await nc.close()
        await jetstream.js.delete_key_value(bucket)


class BytesLimitThatDropsOnce(int):
    def __new__(cls, limit):
        instance = super().__new__(cls, limit)
        instance.armed = True
        instance.dropped = asyncio.Event()
        return instance

    def __le__(self, pending_size):
        if self.armed:
            self.armed = False
            self.dropped.set()
            return True
        return int.__le__(self, pending_size)


@pytest.mark.parametrize("drop", ["pending-queue-full", "pending-bytes-limit"])
async def test_a_slow_consumer_drop_of_the_last_delivery_reaches_a_quiet_watcher(jetstream, drop):  # noqa: F811
    bucket = "watch_" + uuid4().hex
    writer = await jetstream.js.create_key_value(bucket=bucket, history=64)
    await writer.put("p.1.seed", b"seed")
    nc = await nats.connect(jetstream.url)
    nc._error_cb = lambda error: asyncio.sleep(0)
    watermark = await last_revision(jetstream.js, bucket)
    reader = Reader(await watch_kv(await nc.jetstream().key_value(bucket), ">"))
    try:
        async with asyncio.timeout(10):
            await reader.ready.wait()
        await reader.reached(await writer.put("p.1.first", b"1"))
        subscription = reader.watcher._live
        if drop == "pending-queue-full":
            dropped = drop_once_as_slow_consumer(subscription, watermark + 2)
        else:
            limit = BytesLimitThatDropsOnce(subscription._sub._pending_bytes_limit)
            subscription._sub._pending_bytes_limit = limit
            dropped = limit.dropped
        last = await writer.put("p.1.last", b"2")
        async with asyncio.timeout(10):
            await dropped.wait()
        await reader.reached(last, timeout=15)
        assert reader.revisions == [watermark + 1, last]
        assert len(nc._subs) <= 2
    finally:
        await reader.close()
        with contextlib.suppress(Exception):
            await nc.close()
        await jetstream.js.delete_key_value(bucket)
