"""Bounded KV snapshots followed by ordered live updates."""

from __future__ import annotations

import asyncio
import contextlib

from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy
from nats.js.kv import KV_DEL, KV_MARKER_REASON, KV_OP, KV_PURGE, KeyValue


async def watch_kv(
    kv, pattern: str, *, ignore_deletes=False, inactive_threshold=30, snapshot_only=False, start_revision=None
):
    if not isinstance(kv, KeyValue):
        return await kv.watch(
            pattern, ignore_deletes=ignore_deletes, meta_only=True, inactive_threshold=inactive_threshold
        )
    return KvWatcher(kv, pattern, ignore_deletes, inactive_threshold, snapshot_only, start_revision)


class KvWatcher:
    """Pull snapshots in batches so slow readers cannot overflow subscriptions.

    A live ordered subscription resumes after the snapshot's last revision.
    Snapshot consumers are deleted immediately, including on cancellation.
    """

    BATCH_SIZE = 256
    RECONNECT_CHECK_INTERVAL = 1.0

    def __init__(self, kv, pattern, ignore_deletes, inactive_threshold, snapshot_only, start_revision):
        self._kv = kv
        self._pattern = pattern
        self._ignore_deletes = ignore_deletes
        self._inactive_threshold = inactive_threshold
        self._snapshot_only = snapshot_only
        self._start_revision = start_revision
        # The KV API does not expose a bounded snapshot operation. Keep its
        # underlying JetStream connection and subject access in this adapter.
        self._js = kv._js
        self._stream = kv._stream
        self._subject = kv._pre + pattern
        self._snapshot = None
        self._live = None
        self._delivered = 0
        self._gap = False
        self._updates = asyncio.Queue(maxsize=self.BATCH_SIZE)
        self._iterator = self._iterate()

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._iterator.__anext__()

    def _entry(self, msg):
        operation = None
        if msg.headers:
            operation = msg.headers.get(KV_OP)
            if operation is None and KV_MARKER_REASON in msg.headers:
                reason = msg.headers[KV_MARKER_REASON]
                if reason in ("MaxAge", "Purge"):
                    operation = KV_PURGE
                elif reason == "Remove":
                    operation = KV_DEL
                else:
                    return None
        if self._ignore_deletes and operation in (KV_DEL, KV_PURGE):
            return None
        meta = msg.metadata
        return KeyValue.Entry(
            bucket=self._kv._name,
            key=msg.subject[len(self._kv._pre) :],
            value=b"",
            revision=meta.sequence.stream,
            delta=meta.num_pending,
            created=meta.timestamp,
            operation=operation,
        )

    async def _close_subscription(self, subscription):
        if subscription is not None:
            with contextlib.suppress(Exception):
                await subscription.unsubscribe()
            with contextlib.suppress(Exception):
                await self._js.delete_consumer(self._stream, subscription._consumer)

    async def _iterate(self):
        nc = self._js._nc
        reconnects = nc.stats["reconnects"]
        try:
            watermark = 0 if self._snapshot_only else (await self._js.stream_info(self._stream)).state.last_seq
            self._snapshot = await self._js.pull_subscribe(
                self._subject,
                stream=self._stream,
                config=ConsumerConfig(
                    deliver_policy=(
                        DeliverPolicy.BY_START_SEQUENCE
                        if self._start_revision is not None
                        else DeliverPolicy.LAST_PER_SUBJECT
                    ),
                    opt_start_seq=self._start_revision,
                    ack_policy=AckPolicy.NONE,
                    headers_only=True,
                    inactive_threshold=max(self._inactive_threshold, 60),
                ),
                pending_msgs_limit=self.BATCH_SIZE * 2,
                pending_bytes_limit=1024 * 1024,
            )
            pending = (await self._snapshot.consumer_info()).num_pending
            while pending:
                try:
                    messages = await self._snapshot.fetch(min(self.BATCH_SIZE, pending), timeout=5)
                except TimeoutError:
                    messages = []
                # An interrupted snapshot must be rebuilt: an AckNone consumer
                # may have advanced while its client was disconnected.
                if not nc.is_connected or nc.stats["reconnects"] != reconnects:
                    raise RuntimeError("NATS connection changed during KV snapshot")
                for msg in messages:
                    if not self._snapshot_only:
                        watermark = max(watermark, msg.metadata.sequence.stream)
                    entry = self._entry(msg)
                    if entry is not None:
                        yield entry
                if messages:
                    pending = messages[-1].metadata.num_pending
                else:
                    pending = (await self._snapshot.consumer_info()).num_pending
            if not nc.is_connected or nc.stats["reconnects"] != reconnects:
                raise RuntimeError("NATS connection changed during KV snapshot")
            await self._close_subscription(self._snapshot)
            self._snapshot = None
            if self._snapshot_only:
                yield None
                return

            self._delivered = watermark
            self._live = await self._subscribe_live()
            yield None
            while True:
                if self._gap or nc.stats["reconnects"] != reconnects:
                    reconnects = nc.stats["reconnects"]
                    self._gap = False
                    live, self._live = self._live, None
                    await self._close_subscription(live)
                    self._live = await self._subscribe_live()
                try:
                    revision, entry = await asyncio.wait_for(self._updates.get(), self.RECONNECT_CHECK_INTERVAL)
                except TimeoutError:
                    if self._dropped_tail():
                        self._gap = True
                    continue
                if revision <= self._delivered:
                    continue
                self._delivered = revision
                if entry is not None:
                    yield entry
        finally:
            await self._close_subscription(self._snapshot)
            await self._close_subscription(self._live)

    def _dropped_tail(self):
        subscription = getattr(self._live, "_sub", None)
        jsi = getattr(subscription, "_jsi", None)
        if jsi is None or not self._updates.empty() or not subscription._pending_queue.empty():
            return False
        return jsi._sseq > self._delivered

    async def _subscribe_live(self):
        consumer_sequence = 0

        async def receive(msg):
            nonlocal consumer_sequence
            if consumer_sequence is None:
                return
            if msg.metadata.sequence.consumer != consumer_sequence + 1:
                consumer_sequence = None
                self._gap = True
                with contextlib.suppress(asyncio.QueueFull):
                    self._updates.put_nowait((0, None))
                return
            consumer_sequence += 1
            await self._updates.put((msg.metadata.sequence.stream, self._entry(msg)))

        return await self._js.subscribe(
            self._subject,
            stream=self._stream,
            config=ConsumerConfig(opt_start_seq=self._delivered + 1),
            deliver_policy=DeliverPolicy.BY_START_SEQUENCE,
            ordered_consumer=True,
            headers_only=True,
            inactive_threshold=self._inactive_threshold,
            cb=receive,
        )

    async def stop(self):
        await self._iterator.aclose()
