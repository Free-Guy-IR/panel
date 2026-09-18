import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from PasarGuardNodeBridge import Health, NodeAPIError
from sqlalchemy import bindparam, delete, insert, select, update

from app.db import GetDB
from app.fork.models.traffic_log import TrafficLogRecord, TrafficLogState
from app.fork.traffic_log.identity import IdentityCache
from app.fork.traffic_log.parse import Event, SingboxFlows, parse_access_line, parse_singbox_line
from app.node import node_manager
from app.utils.logger import get_logger
from config import job_settings, runtime_settings, server_settings
from role import Role

logger = get_logger("traffic-log")

STREAM_QUEUE_SIZE = 5_000
TAP_QUEUE_SIZE = 2_000
SUBSCRIBER_QUEUE_SIZE = 1_000
SUBSCRIBER_CAP = 32
BUCKET_CAP = 50_000
SEEN_IDS_CAP = 50_000
BUCKET_MINUTES = 5
NO_REPORTS_AFTER = timedelta(seconds=60)
IDLE_PROBE_SECONDS = 30.0
CORE_UPTIME_SLACK = 5.0
DIRECT_HANDOFF_TIMEOUT = 5.0
DIRECT_HANDOFF_POLL = 0.05
DIRECT_POLL_SECONDS = 0.25
VIEWER_HANDOFF_TIMEOUT = 0.25
DROP_NOTICE_INTERVAL = 1.0
UPDATE_CHUNK = 1_000
STATE_ROW_ID = 1
RESOLVE_DEBOUNCE_SECONDS = 0.5
STATE_REFRESH_SECONDS = 15.0
CEILING_SLACK = 10_000
DEFAULT_RETENTION_HOURS = 48
RETENTION_MIN_HOURS = 1
RETENTION_MAX_HOURS = 720


def clamp_retention_hours(hours: int | None) -> int:
    if hours is None:
        return DEFAULT_RETENTION_HOURS
    return max(RETENTION_MIN_HOURS, min(RETENTION_MAX_HOURS, int(hours)))


class DetachSignal:
    __slots__ = ()


DETACHED = DetachSignal()

STATE_ATTACHING = "attaching"
STATE_COLLECTING = "collecting"
STATE_ERROR = "error"
STATE_DETACHED = "detached"
STATE_PAUSED = "paused"
STATE_UNAVAILABLE = "unavailable"
STATE_NO_REPORTS = "no_reports"


@dataclass(slots=True, eq=False)
class NodeState:
    node_id: int
    name: str
    state: str = STATE_DETACHED
    since: datetime = field(default_factory=lambda: datetime.now(UTC))
    lines: int = 0
    events: int = 0
    dropped_records: int = 0
    dropped_live: int = 0
    dropped_viewer: int = 0
    stream_full: int = 0
    restreams: int = 0
    records: int = 0
    last_event_at: datetime | None = None
    detail: str | None = None
    node: Any = None
    core_boot: float | None = None
    baseline_retry: bool = False
    flows: SingboxFlows = field(default_factory=SingboxFlows)

    def transition(self, state: str, detail: str | None = None) -> None:
        self.state = state
        self.detail = detail
        self.since = datetime.now(UTC)

    def reported_state(self) -> str:
        if self.state != STATE_COLLECTING or self.lines == 0:
            return self.state
        reference = self.last_event_at or self.since
        if datetime.now(UTC) - reference >= NO_REPORTS_AFTER:
            return STATE_NO_REPORTS
        return self.state

    def snapshot(self) -> dict:
        return {
            "node_id": self.node_id,
            "node": self.name,
            "state": self.reported_state(),
            "since": self.since,
            "lines": self.lines,
            "events": self.events,
            "dropped": self.dropped_records + self.stream_full,
            "dropped_records": self.dropped_records,
            "dropped_live": self.dropped_live,
            "dropped_viewer": self.dropped_viewer,
            "stream_full": self.stream_full,
            "restreams": self.restreams,
            "records": self.records,
            "last_event_at": self.last_event_at,
            "detail": self.detail,
        }


@dataclass(slots=True, eq=False)
class Subscriber:
    queue: asyncio.Queue
    user_id: int | None
    node_id: int | None
    admin_id: int | None
    dropped: int = 0
    last_notice: float = 0.0


@dataclass(slots=True, eq=False)
class Bucket:
    first_seen: datetime
    last_seen: datetime
    hits: int
    route: str
    row_id: int | None = None
    flushed_hits: int = 0
    last_ingest: int = 0


BucketKey = tuple[int | None, str | None, int, str, str, int, str, str, bool, datetime]


def bucket_start_of(at: datetime) -> datetime:
    return at.replace(minute=at.minute - at.minute % BUCKET_MINUTES, second=0, microsecond=0)


def _offer(queue: asyncio.Queue, item: Any) -> bool:
    try:
        queue.put_nowait(item)
        return False
    except asyncio.QueueFull:
        with contextlib.suppress(asyncio.QueueEmpty):
            queue.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(item)
        return True


class TrafficCollector:
    def __init__(self):
        if server_settings.workers > 1:
            self.available = False
            self.unavailable_reason = (
                "the panel runs more than one web worker, so no single process owns the node log streams"
            )
        elif not runtime_settings.role.runs_node:
            self.available = False
            self.unavailable_reason = "this process does not run nodes, so it cannot read their log streams"
        elif runtime_settings.role is not Role.ALL_IN_ONE:
            self.available = False
            self.unavailable_reason = (
                "the panel is split across roles, and traffic log collection needs the process that serves the panel "
                "to be the one that owns the node log streams"
            )
        else:
            self.available = True
            self.unavailable_reason = None
        self.enabled = job_settings.traffic_log_enabled
        self.retention_hours = DEFAULT_RETENTION_HOURS
        self._state_loaded = False
        self.purge_stats: dict = {
            "last_purge_at": None,
            "purged_expired": 0,
            "purged_over_ceiling": 0,
            "ceiling_active": False,
            "purge_incomplete": False,
        }
        self.identity = IdentityCache()
        self._started = False
        self._states: dict[int, NodeState] = {}
        self._readers: dict[int, asyncio.Task] = {}
        self._taps: dict[int, set[asyncio.Queue]] = {}
        self._direct: dict[int, set[asyncio.Event]] = {}
        self._direct_pumps: dict[int, asyncio.Task] = {}
        self._subscribers: set[Subscriber] = set()
        self._buckets: dict[BucketKey, Bucket] = {}
        self._seen_ids: set[int] = set()
        self._unresolved: set[int] = set()
        self._resolve_task: asyncio.Task | None = None
        self._max_row_id = 0
        self._ceiling_floor = 0
        self._eviction_task: asyncio.Task | None = None
        self._purge_generation = 0
        self._ingest_seq = 0
        self._flush_task: asyncio.Task | None = None
        self._rows_dirty = False
        self._adopt_until: datetime | None = bucket_start_of(datetime.now(UTC))
        self._flush_lock = asyncio.Lock()
        self._attach_lock = asyncio.Lock()

    async def start(self) -> None:
        await self.load_settings()
        if not self.available:
            logger.warning(f"traffic log collector is unavailable: {self.unavailable_reason}")
            return
        self._state_loaded = True
        async with self._attach_lock:
            self._started = True
            if self._flush_task is None or self._flush_task.done():
                self._flush_task = asyncio.create_task(self._flush_loop(), name="traffic-log-flush")
            if self.enabled:
                await self._attach_healthy()
        logger.info(
            f"traffic log collector started (enabled={self.enabled}, "
            f"retention={self.retention_hours}h, nodes={len(self._readers)})"
        )

    async def stop(self) -> None:
        async with self._attach_lock:
            self._started = False
            for node_id in list(self._readers):
                await self._detach(node_id, STATE_DETACHED, "panel shutting down")
        for node_id, pump in list(self._direct_pumps.items()):
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pump
            self._direct_pumps.pop(node_id, None)
        for name in ("_flush_task", "_resolve_task", "_eviction_task"):
            task = getattr(self, name)
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
                setattr(self, name, None)
        try:
            await self._flush()
        except Exception:
            logger.exception("traffic log final flush failed")

    async def load_settings(self) -> None:
        state = await self._load_state()
        if state is not None:
            self.enabled, self.retention_hours = state

    async def set_retention_hours(self, hours: int) -> None:
        wanted = clamp_retention_hours(hours)
        await self._persist_state(retention_hours=wanted)
        self.retention_hours = wanted

    def ingest_watermark(self) -> int:
        return self._ingest_seq

    def tracked_row_ids(self) -> set[int]:
        return {bucket.row_id for bucket in self._buckets.values() if bucket.row_id is not None}

    @staticmethod
    def _carried_over(bucket: Bucket, boundary: datetime) -> Bucket:
        return Bucket(
            first_seen=min(max(bucket.first_seen, boundary), bucket.last_seen)
            if bucket.row_id is None
            else bucket.first_seen,
            last_seen=bucket.last_seen,
            hits=bucket.hits,
            route=bucket.route,
            row_id=bucket.row_id,
            flushed_hits=bucket.flushed_hits,
            last_ingest=bucket.last_ingest,
        )

    def reseed_row_watermark(self, max_row_id: int, total_rows: int) -> None:
        self._max_row_id = max_row_id
        self._ceiling_floor = min(self._ceiling_floor, max_row_id)
        if total_rows == 0:
            self._seen_ids = set()

    def detach_missing_rows(self, surviving: set[int]) -> int:
        detached = 0
        for bucket in self._buckets.values():
            if bucket.row_id is not None and bucket.row_id in surviving:
                continue
            if bucket.row_id is not None:
                detached += 1
            bucket.row_id = None
            bucket.flushed_hits = 0
        return detached

    def mark_rows_dirty(self) -> None:
        self._rows_dirty = True

    def clear_rows_dirty(self) -> None:
        self._rows_dirty = False

    def detach_rows(self, gone: set[int]) -> int:
        detached = 0
        for bucket in self._buckets.values():
            if bucket.row_id is not None and bucket.row_id in gone:
                bucket.row_id = None
                bucket.flushed_hits = 0
                detached += 1
        return detached

    def forget_buckets(
        self,
        cutoff: datetime | None = None,
        *,
        keep_after: datetime | None = None,
        since_ingest: int | None = None,
        keep_stored_rows: bool = False,
    ) -> None:
        self._purge_generation += 1
        if self._eviction_task is not None and not self._eviction_task.done():
            self._eviction_task.cancel()

        def survives(bucket: Bucket, boundary: datetime) -> bool:
            if keep_stored_rows and bucket.row_id is not None:
                return True
            if bucket.hits <= 0:
                return False
            if since_ingest is not None and bucket.last_ingest > since_ingest:
                return True
            return bucket.last_seen >= boundary

        if cutoff is None:
            survivors = {}
            if keep_after is not None:
                for key, bucket in self._buckets.items():
                    if survives(bucket, keep_after):
                        survivors[key] = self._carried_over(bucket, keep_after)
            self._buckets = survivors
        else:
            for key in [key for key in self._buckets if key[-1] < cutoff]:
                bucket = self._buckets[key]
                if survives(bucket, cutoff):
                    self._buckets[key] = self._carried_over(bucket, cutoff)
                else:
                    self._buckets.pop(key, None)

    @contextlib.asynccontextmanager
    async def suspend_flush(self) -> AsyncIterator[None]:
        async with self._flush_lock:
            yield

    async def set_enabled(self, enabled: bool) -> None:
        await self._persist_state(enabled=enabled)
        await self.apply_enabled(enabled)

    async def apply_enabled(self, enabled: bool) -> None:
        async with self._attach_lock:
            self.enabled = enabled
            if not enabled:
                for node_id in list(self._readers):
                    await self._detach(node_id, STATE_PAUSED, "collection paused")
                for state in self._states.values():
                    if state.state not in (STATE_ERROR, STATE_PAUSED):
                        state.transition(STATE_PAUSED, "collection paused")
                self._broadcast({"control": "paused"})
                return
            if self._started and self.available:
                await self._attach_healthy()

    async def ensure_attached(self, node_id: int, node, name: str) -> None:
        if not self.available:
            async with self._attach_lock:
                state = self._states.get(node_id)
                if state is None:
                    state = NodeState(node_id=node_id, name=name)
                    self._states[node_id] = state
                state.name = name
                if state.state != STATE_UNAVAILABLE:
                    state.transition(STATE_UNAVAILABLE, self.unavailable_reason)
            return
        if not self._started:
            return
        async with self._attach_lock:
            await self._attach(node_id, node, name)

    async def _attach(self, node_id: int, node, name: str) -> None:
        if not self.available or not self._started:
            return
        state = self._states.get(node_id)
        if state is None:
            state = NodeState(node_id=node_id, name=name)
            self._states[node_id] = state
        state.name = name
        if not self.enabled:
            if state.state != STATE_PAUSED:
                state.transition(STATE_PAUSED, "collection paused")
            return
        if self.is_attached(node_id):
            if state.node is node:
                return
            await self._detach(node_id, STATE_DETACHED, "node connection replaced")
        state.node = node
        state.transition(STATE_ATTACHING)
        self._readers[node_id] = asyncio.create_task(self._read(node_id, node), name=f"traffic-log-reader-{node_id}")

    async def detach(self, node_id: int) -> None:
        async with self._attach_lock:
            await self._detach(node_id, STATE_DETACHED, "detached")

    def is_attached(self, node_id: int) -> bool:
        task = self._readers.get(node_id)
        return task is not None and not task.done()

    @contextlib.asynccontextmanager
    async def direct_reader(self, node_id: int) -> AsyncIterator[asyncio.Event]:
        stop = asyncio.Event()
        self._direct.setdefault(node_id, set()).add(stop)
        try:
            yield stop
        finally:
            readers = self._direct.get(node_id)
            if readers is not None:
                readers.discard(stop)
                if not readers:
                    self._direct.pop(node_id, None)

    async def ensure_direct_pump(self, node_id: int, node, kwargs: dict) -> bool:
        if self.is_attached(node_id):
            return False
        task = self._direct_pumps.get(node_id)
        if task is not None and not task.done():
            return True
        self._direct_pumps[node_id] = asyncio.create_task(
            self._direct_pump(node_id, node, kwargs), name=f"traffic-log-direct-{node_id}"
        )
        return True

    async def _direct_pump(self, node_id: int, node, kwargs: dict) -> None:
        stop = asyncio.Event()
        self._direct.setdefault(node_id, set()).add(stop)
        try:
            async with node.stream_logs(**kwargs) as queue:
                while not stop.is_set():
                    if not self._taps.get(node_id):
                        await asyncio.sleep(DIRECT_POLL_SECONDS)
                        if not self._taps.get(node_id):
                            return
                        continue
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=DIRECT_POLL_SECONDS)
                    except TimeoutError:
                        continue
                    self._push_taps(node_id, item, self._states.get(node_id))
        except asyncio.CancelledError:
            raise
        except NodeAPIError as error:
            self._push_taps(node_id, error, None)
        except Exception as error:
            self._push_taps(node_id, NodeAPIError(-1, f"{type(error).__name__}: {error}"), None)
        finally:
            readers = self._direct.get(node_id)
            if readers is not None:
                readers.discard(stop)
                if not readers:
                    self._direct.pop(node_id, None)
            if self._direct_pumps.get(node_id) is asyncio.current_task():
                self._direct_pumps.pop(node_id, None)

    async def _stop_direct_readers(self, node_id: int) -> bool:
        readers = self._direct.get(node_id)
        if not readers:
            return True
        for stop in list(readers):
            stop.set()
        deadline = time.monotonic() + DIRECT_HANDOFF_TIMEOUT
        while self._direct.get(node_id) and time.monotonic() < deadline:
            await asyncio.sleep(DIRECT_HANDOFF_POLL)
        return not self._direct.get(node_id)

    @contextlib.asynccontextmanager
    async def tap(self, node_id: int) -> AsyncIterator[asyncio.Queue]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=TAP_QUEUE_SIZE)
        self._taps.setdefault(node_id, set()).add(queue)
        if not self.is_attached(node_id):
            _offer(queue, DETACHED)
        try:
            yield queue
        finally:
            taps = self._taps.get(node_id)
            if taps is not None:
                taps.discard(queue)
                if not taps:
                    self._taps.pop(node_id, None)

    @contextlib.asynccontextmanager
    async def subscribe(
        self, *, user_id: int | None = None, node_id: int | None = None, admin_id: int | None = None
    ) -> AsyncIterator[asyncio.Queue]:
        subscriber = Subscriber(
            queue=asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE), user_id=user_id, node_id=node_id, admin_id=admin_id
        )
        if len(self._subscribers) >= SUBSCRIBER_CAP:
            subscriber.queue.put_nowait(
                {
                    "control": "too_many_viewers",
                    "reason": f"{SUBSCRIBER_CAP} live views are already open; close one before opening another",
                }
            )
            yield subscriber.queue
            return
        if not self.available:
            subscriber.queue.put_nowait({"control": "unavailable", "reason": self.unavailable_reason})
        elif not self.enabled:
            subscriber.queue.put_nowait({"control": "paused"})
        self._subscribers.add(subscriber)
        try:
            yield subscriber.queue
        finally:
            self._subscribers.discard(subscriber)

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "available": self.available,
            "reason": self.unavailable_reason,
            "nodes": [state.snapshot() for state in sorted(self._states.values(), key=lambda s: s.node_id)],
        }

    def record_purge(self, expired: int, over_ceiling: int, ceiling_active: bool, incomplete: bool = False) -> None:
        self.purge_stats = {
            "last_purge_at": datetime.now(UTC),
            "purged_expired": expired,
            "purged_over_ceiling": over_ceiling,
            "ceiling_active": ceiling_active,
            "purge_incomplete": incomplete,
        }

    def record_manual_purge(self, expired: int, incomplete: bool) -> None:
        self.purge_stats = {
            **self.purge_stats,
            "last_purge_at": datetime.now(UTC),
            "purged_expired": expired,
            "purge_incomplete": incomplete,
        }

    def note_ceiling_floor(self, threshold: int, incomplete: bool) -> None:
        if not incomplete and threshold > self._ceiling_floor:
            self._ceiling_floor = threshold

    def _maybe_evict(self) -> None:
        ceiling = job_settings.traffic_log_max_records
        if ceiling <= 0:
            return
        threshold = self._max_row_id - ceiling
        if threshold <= self._ceiling_floor + CEILING_SLACK:
            return
        if self._eviction_task is not None and not self._eviction_task.done():
            return
        self._eviction_task = asyncio.create_task(self._evict(threshold), name="traffic-log-evict")

    async def _evict(self, threshold: int) -> None:
        from app.fork.jobs.traffic_log_purge import enforce_ceiling, surviving_row_ids

        generation = self._purge_generation
        try:
            async with self._flush_lock:
                if generation != self._purge_generation:
                    return
                async with GetDB() as db:
                    self._rows_dirty = True
                    removed, incomplete = await enforce_ceiling(db, threshold)
                    tracked = {row_id for row_id in self.tracked_row_ids() if row_id <= threshold}
                    if removed and tracked:
                        gone = tracked - await surviving_row_ids(db, tracked)
                        if gone:
                            logger.info(
                                f"traffic log ceiling eviction detached {self.detach_rows(gone)} bucket(s) "
                                "whose stored row is gone"
                            )
                    self._rows_dirty = False
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("traffic log ceiling eviction failed")
            return
        self.note_ceiling_floor(threshold, incomplete)
        if not removed:
            return
        self.record_purge(0, removed, True, incomplete)
        logger.info(
            f"traffic log ceiling eviction removed {removed} records at or below id {threshold}"
            + (" (more remain)" if incomplete else "")
        )

    async def effective_retention_hours(self) -> int:
        if self._state_loaded:
            return self.retention_hours
        state = await self._load_state()
        if state is None:
            return self.retention_hours
        return state[1]

    async def _load_state(self) -> tuple[bool, int] | None:
        defaults = (job_settings.traffic_log_enabled, DEFAULT_RETENTION_HOURS)
        try:
            async with GetDB() as db:
                stored = (
                    await db.execute(
                        select(TrafficLogState.enabled, TrafficLogState.retention_hours).where(
                            TrafficLogState.id == STATE_ROW_ID
                        )
                    )
                ).first()
        except Exception:
            if self._state_loaded:
                logger.exception("could not read traffic_log_state; keeping the state already in force")
                return None
            logger.exception("could not read traffic_log_state; starting with collection disabled")
            return (False, defaults[1])
        if stored is None:
            return defaults
        enabled, retention_hours = stored
        return bool(enabled), clamp_retention_hours(retention_hours)

    async def _persist_state(self, **values) -> None:
        now = datetime.now(UTC)
        async with GetDB() as db:
            exists = await db.scalar(select(TrafficLogState.id).where(TrafficLogState.id == STATE_ROW_ID))
            if exists is None:
                created = {"enabled": self.enabled, "retention_hours": self.retention_hours, **values}
                await db.execute(insert(TrafficLogState).values(id=STATE_ROW_ID, updated_at=now, **created))
            else:
                await db.execute(
                    update(TrafficLogState).where(TrafficLogState.id == STATE_ROW_ID).values(updated_at=now, **values)
                )
            await db.commit()

    async def _attach_healthy(self) -> None:
        for node_id, node in await node_manager.get_healthy_nodes():
            await self._attach(node_id, node, getattr(node, "name", None) or str(node_id))

    async def _detach(self, node_id: int, state_name: str, detail: str | None) -> None:
        task = self._readers.get(node_id)
        state = self._states.get(node_id)
        if state is not None:
            state.node = None
            state.transition(state_name, detail)
        if task is None:
            self._readers.pop(node_id, None)
            return
        if task is not asyncio.current_task() and not task.done():
            task.cancel()
            with contextlib.suppress(Exception):
                await asyncio.wait([task])
        self._readers.pop(node_id, None)
        self._push_taps(node_id, DETACHED, state)

    async def _core_boot(self, node) -> float | None:
        try:
            stats = await node.get_backend_stats()
        except Exception:
            return None
        uptime = getattr(stats, "uptime", None)
        if uptime is None:
            return None
        return time.monotonic() - uptime

    async def _core_restarted(self, node, state: NodeState) -> bool:
        boot = await self._core_boot(node)
        if boot is None:
            return False
        if state.core_boot is None:
            state.core_boot = boot
            retry = state.baseline_retry
            state.baseline_retry = False
            return retry
        if boot > state.core_boot + CORE_UPTIME_SLACK:
            state.core_boot = boot
            return True
        return False

    async def _read(self, node_id: int, node) -> None:
        state = self._states[node_id]
        ended = STATE_DETACHED
        detail: str | None = "stream ended"
        try:
            while True:
                reopen = False
                baseline = await self._core_boot(node)
                if not await self._stop_direct_readers(node_id):
                    ended = STATE_ERROR
                    detail = "a raw log viewer still holds this node's log stream"
                    logger.warning(f"Traffic log: node {node_id} not attached because {detail}")
                    return
                async with node.stream_logs(max_queue_size=STREAM_QUEUE_SIZE) as queue:
                    state.core_boot = baseline
                    state.baseline_retry = baseline is None
                    state.transition(STATE_COLLECTING)
                    while True:
                        try:
                            item = await asyncio.wait_for(queue.get(), timeout=IDLE_PROBE_SECONDS)
                        except TimeoutError:
                            if await node.get_health() != Health.HEALTHY:
                                detail = "node is no longer healthy"
                                return
                            if await self._core_restarted(node, state):
                                reopen = True
                                break
                            continue
                        if queue.qsize() >= STREAM_QUEUE_SIZE - 1:
                            state.stream_full += 1
                        if isinstance(item, NodeAPIError):
                            ended = STATE_ERROR
                            detail = str(item)
                            self._push_taps(node_id, item, state)
                            return
                        self._ingest(state, str(item))
                if not reopen:
                    return
                state.restreams += 1
                state.transition(STATE_ATTACHING, "core restarted; reopening the log stream")
                logger.info(f"[{state.name}] traffic log stream reopened after a core restart")
        except asyncio.CancelledError:
            raise
        except NodeAPIError as exc:
            ended = STATE_ERROR
            detail = str(exc)
        except Exception as exc:
            ended = STATE_ERROR
            detail = f"{type(exc).__name__}: {exc}"
            logger.exception(f"[{state.name}] traffic log reader failed")
        finally:
            if self._readers.get(node_id) is asyncio.current_task():
                self._readers.pop(node_id, None)
                state.node = None
                state.transition(ended, detail)
                self._push_taps(node_id, DETACHED, state)
                if ended == STATE_ERROR:
                    logger.warning(f"[{state.name}] traffic log collection stopped: {detail}")

    def _ingest(self, state: NodeState, line: str) -> None:
        state.lines += 1
        self._push_taps(state.node_id, line, state)
        seen_at = datetime.now(UTC)
        event = parse_access_line(line, state.node_id, seen_at)
        if event is None:
            event = parse_singbox_line(line, state.node_id, seen_at, state.flows)
        if event is None:
            return
        state.events += 1
        state.last_event_at = event.at
        if event.user_id is not None:
            if len(self._seen_ids) < SEEN_IDS_CAP:
                self._seen_ids.add(event.user_id)
            if not self.identity.is_resolved(event.user_id):
                self._note_unresolved(event.user_id)
        self._fan_out(event, state)
        self._fold(event, state)

    def _note_unresolved(self, user_id: int) -> None:
        if len(self._unresolved) >= SEEN_IDS_CAP:
            return
        self._unresolved.add(user_id)
        if self._resolve_task is None or self._resolve_task.done():
            self._resolve_task = asyncio.create_task(self._resolve_unresolved(), name="traffic-log-identity")

    async def _resolve_unresolved(self) -> None:
        await asyncio.sleep(RESOLVE_DEBOUNCE_SECONDS)
        async with self._flush_lock:
            pending = self._unresolved
            self._unresolved = set()
            wanted = {user_id for user_id in pending if not self.identity.is_resolved(user_id)}
            if not wanted:
                return
            try:
                async with GetDB() as db:
                    await self.identity.resolve_many(db, wanted)
            except Exception:
                logger.exception("traffic log identity resolution failed")

    def note_viewer_drop(self, node_id: int) -> None:
        state = self._states.get(node_id)
        if state is not None:
            state.dropped_viewer += 1

    def _push_taps(self, node_id: int, item: Any, state: NodeState | None) -> None:
        taps = self._taps.get(node_id)
        if not taps:
            return
        for queue in taps:
            if _offer(queue, item) and state is not None:
                state.dropped_viewer += 1

    def _fan_out(self, event: Event, state: NodeState) -> None:
        if not self._subscribers:
            return
        payload = None
        now = time.monotonic()
        for subscriber in self._subscribers:
            if subscriber.node_id is not None and subscriber.node_id != event.node_id:
                continue
            if subscriber.user_id is not None and subscriber.user_id != event.user_id:
                continue
            if subscriber.admin_id is not None and not self._owned_by(event, subscriber.admin_id):
                continue
            if payload is None:
                payload = self._live_payload(event, state)
            if _offer(subscriber.queue, payload):
                subscriber.dropped += 1
                state.dropped_live += 1
                if now - subscriber.last_notice >= DROP_NOTICE_INTERVAL:
                    announced = subscriber.dropped
                    subscriber.last_notice = now
                    if _offer(subscriber.queue, {"control": "dropped", "count": announced}):
                        state.dropped_live += 1
                        subscriber.dropped = 1
                    else:
                        subscriber.dropped = 0

    def _owned_by(self, event: Event, admin_id: int) -> bool:
        return self.identity.fresh_owner_of(event.user_id) == admin_id

    def _live_payload(self, event: Event, state: NodeState) -> dict:
        username = self.identity.username_of(event.user_id) if event.user_id is not None else event.user_label
        return {
            "at": event.at.isoformat(),
            "user_id": event.user_id,
            "username": username,
            "node_id": event.node_id,
            "node": state.name,
            "inbound": event.inbound,
            "host": event.host,
            "port": event.port,
            "protocol": event.protocol,
            "route": event.route,
            "refused": event.refused,
        }

    def _broadcast(self, control: dict) -> None:
        for subscriber in self._subscribers:
            if _offer(subscriber.queue, control):
                subscriber.dropped += 1

    def _fold(self, event: Event, state: NodeState) -> None:
        key: BucketKey = (
            event.user_id,
            event.user_label,
            event.node_id,
            event.inbound,
            event.host,
            event.port,
            event.protocol,
            event.route,
            event.refused,
            bucket_start_of(event.at),
        )
        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) >= BUCKET_CAP:
                state.dropped_records += 1
                return
            self._ingest_seq += 1
            self._buckets[key] = Bucket(
                first_seen=event.at, last_seen=event.at, hits=1, route=event.route, last_ingest=self._ingest_seq
            )
            return
        self._ingest_seq += 1
        bucket.hits += 1
        bucket.last_seen = max(bucket.last_seen, event.at)
        bucket.last_ingest = self._ingest_seq

    async def _flush_loop(self) -> None:
        refreshed_at = time.monotonic()
        while True:
            await asyncio.sleep(max(1, job_settings.traffic_log_flush_seconds))
            try:
                await self._flush()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("traffic log flush failed")
            if time.monotonic() - refreshed_at < STATE_REFRESH_SECONDS:
                continue
            refreshed_at = time.monotonic()
            try:
                await self._refresh_state()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("traffic log state refresh failed")

    async def _refresh_state(self) -> None:
        self.identity.prune()
        state = await self._load_state()
        if state is None:
            return
        enabled, retention_hours = state
        self.retention_hours = retention_hours
        if enabled == self.enabled:
            return
        logger.info(f"traffic log collection was changed elsewhere to enabled={enabled}; applying it here")
        await self.apply_enabled(enabled)

    async def _flush(self) -> None:
        async with self._flush_lock:
            await self._flush_once()

    async def _resync_rows(self, db) -> None:
        from app.fork.jobs.traffic_log_purge import surviving_row_ids

        tracked = self.tracked_row_ids()
        gone: set[int] = set()
        if tracked:
            gone = tracked - await surviving_row_ids(db, tracked)
        self._rows_dirty = False
        if gone:
            logger.info(f"traffic log flush detached {self.detach_rows(gone)} bucket(s) whose stored row is gone")

    def _adoptable(self, boundary: datetime) -> dict:
        return {key: bucket for key, bucket in self._buckets.items() if bucket.row_id is None and key[-1] <= boundary}

    @staticmethod
    def _record_key(record: TrafficLogRecord) -> BucketKey:
        start = record.bucket_start
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        return (
            record.user_id,
            record.user_label,
            record.node_id,
            record.inbound_tag,
            record.host,
            record.port,
            record.protocol,
            record.route,
            record.refused,
            start,
        )

    @staticmethod
    def _aware(moment: datetime) -> datetime:
        return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)

    async def _merge_duplicate_rows(self, db, grouped: dict) -> dict:
        stored = {}
        extras = []
        merged_windows = set()
        referenced = self.tracked_row_ids()
        for identity, records in grouped.items():
            records.sort(key=lambda record: record.id)
            primary = records[0]
            stored[identity] = primary
            if len(records) == 1 or any(record.id in referenced for record in records):
                continue
            primary.hits = sum(record.hits for record in records)
            primary.first_seen = min(self._aware(record.first_seen) for record in records)
            primary.last_seen = max(self._aware(record.last_seen) for record in records)
            extras.extend(record.id for record in records[1:])
            merged_windows.add(identity[-1])
        if extras:
            await db.execute(delete(TrafficLogRecord).where(TrafficLogRecord.id.in_(extras)))
            await db.commit()
            logger.warning(
                f"traffic log merged {len(extras)} duplicate row(s) into the oldest row of their bucket "
                f"in window(s) {sorted(window.isoformat() for window in merged_windows)}"
            )
        return stored

    async def _adopt_stored_rows(self, db) -> None:
        boundary = self._adopt_until
        if boundary is None:
            return
        windows = {key[-1] for key in self._adoptable(boundary)}
        if windows:
            rows = await db.scalars(select(TrafficLogRecord).where(TrafficLogRecord.bucket_start.in_(windows)))
            grouped: dict[BucketKey, list[TrafficLogRecord]] = {}
            for record in rows.all():
                grouped.setdefault(self._record_key(record), []).append(record)
            stored = await self._merge_duplicate_rows(db, grouped)
            adopted = 0
            for key, bucket in self._adoptable(boundary).items():
                record = stored.get(key)
                if record is None:
                    continue
                bucket.row_id = record.id
                bucket.hits += record.hits
                bucket.flushed_hits = record.hits
                stored_first_seen = record.first_seen
                if stored_first_seen.tzinfo is None:
                    stored_first_seen = stored_first_seen.replace(tzinfo=UTC)
                bucket.first_seen = min(bucket.first_seen, stored_first_seen)
                self._max_row_id = max(self._max_row_id, record.id)
                adopted += 1
            if adopted:
                logger.info(f"traffic log flush adopted {adopted} row(s) written before this process started")
        if bucket_start_of(datetime.now(UTC)) > boundary:
            self._adopt_until = None

    async def _flush_once(self) -> None:
        seen = self._seen_ids
        self._seen_ids = set()
        wanted = seen | {key[0] for key in self._buckets if key[0] is not None}
        stale = self.identity.stale(wanted)
        if self._rows_dirty or self._adopt_until is not None:
            async with GetDB() as db:
                if self._rows_dirty:
                    await self._resync_rows(db)
                await self._adopt_stored_rows(db)
        fresh = [(key, bucket) for key, bucket in self._buckets.items() if bucket.row_id is None]
        changed = [
            (key, bucket, bucket.hits, bucket.last_seen)
            for key, bucket in self._buckets.items()
            if bucket.row_id is not None and bucket.hits != bucket.flushed_hits
        ]
        if not stale and not fresh and not changed:
            self._expire_buckets()
            return
        async with GetDB() as db:
            if stale:
                await self.identity.resolve_many(db, stale)
            if fresh or changed:
                await self._write_buckets(db, fresh, changed)
        self._expire_buckets()

    async def _write_buckets(self, db, fresh: list, changed: list) -> None:
        records = []
        fresh_hits = []
        for key, bucket in fresh:
            user_id, user_label, node_id, inbound, host, port, protocol, route, refused, bucket_start = key
            records.append(
                TrafficLogRecord(
                    bucket_start=bucket_start,
                    user_id=user_id,
                    user_label=user_label,
                    node_id=node_id,
                    inbound_tag=inbound,
                    host=host,
                    port=port,
                    protocol=protocol,
                    refused=refused,
                    route=route,
                    first_seen=bucket.first_seen,
                    last_seen=bucket.last_seen,
                    hits=bucket.hits,
                )
            )
            fresh_hits.append(bucket.hits)
        if records:
            db.add_all(records)
            await db.flush()
            self._rows_dirty = True
            for (_, bucket), record, hits in zip(fresh, records, fresh_hits, strict=True):
                bucket.row_id = record.id
                bucket.flushed_hits = hits
                self._max_row_id = max(self._max_row_id, record.id)
        if changed:
            statement = update(TrafficLogRecord.__table__).where(TrafficLogRecord.__table__.c.id == bindparam("row_id"))
            rows = [
                {"row_id": bucket.row_id, "hits": hits, "last_seen": last_seen}
                for _, bucket, hits, last_seen in changed
            ]
            for start in range(0, len(rows), UPDATE_CHUNK):
                await db.execute(statement, rows[start : start + UPDATE_CHUNK])
        await db.commit()
        self._rows_dirty = False
        for key, _ in fresh:
            state = self._states.get(key[2])
            if state is not None:
                state.records += 1
        for _, bucket, hits, _ in changed:
            bucket.flushed_hits = hits
        self._maybe_evict()

    def _expire_buckets(self) -> None:
        current = bucket_start_of(datetime.now(UTC))
        cutoff = datetime.now(UTC) - timedelta(hours=self.retention_hours)
        expired = [
            key
            for key, bucket in self._buckets.items()
            if key[-1] < cutoff
            or (key[-1] < current and bucket.row_id is not None and bucket.hits == bucket.flushed_hits)
        ]
        for key in expired:
            bucket = self._buckets.pop(key)
            if key[-1] < cutoff and bucket.row_id is None:
                state = self._states.get(key[2])
                if state is not None:
                    state.dropped_records += 1


collector = TrafficCollector()
