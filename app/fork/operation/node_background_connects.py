import asyncio
import contextlib
import weakref
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from dataclasses import dataclass

from app.fork.operation.node_connect_generation import bump_connect_generation, connect_generation
from app.fork.operation.node_connect_jobs import connects_in_flight, is_retiring
from app.fork.operation.node_extras import _connect_lock
from app.fork.operation.node_messages import describe_error
from app.nats import needs_shared_bridge_memory
from app.node import node_manager
from app.node.manager_sync import publish_node_sync
from app.utils.logger import get_logger

logger = get_logger("node-connect")

REQUEST_WAIT_CAP_SECONDS = 30
REQUEST_WAIT_FALLBACK_SECONDS = 15

type LateWrite = Callable[[int, dict], Awaitable[bool]]


@dataclass(frozen=True)
class ConnectMark:
    generation: int
    epoch: int | None


_marks: weakref.WeakKeyDictionary[asyncio.Task, ConnectMark] = weakref.WeakKeyDictionary()
_pending: dict[int, set[asyncio.Task]] = {}
_background: dict[asyncio.Task, tuple[int, asyncio.Task]] = {}


def _forget_unit(node_id: int, unit: asyncio.Task) -> None:
    units = _pending.get(node_id)
    if units is None:
        return
    units.discard(unit)
    if not units:
        _pending.pop(node_id, None)


def start_connect(node_id: int, work: Coroutine) -> asyncio.Task:
    unit = asyncio.create_task(work, name=f"connect-{node_id}")
    _pending.setdefault(node_id, set()).add(unit)
    unit.add_done_callback(lambda done: _forget_unit(node_id, done))
    return unit


def _another_connect_pending(node_id: int, unit: asyncio.Task) -> bool:
    if any(other is not unit and not other.done() for other in _pending.get(node_id, ())):
        return True
    return connects_in_flight(node_id)


def _seconds(node) -> int:
    return max(1, int(getattr(node, "internal_timeout", None) or REQUEST_WAIT_FALLBACK_SECONDS))


def request_wait(nodes: Iterable) -> float:
    return float(
        min(REQUEST_WAIT_CAP_SECONDS, max((_seconds(node) for node in nodes), default=REQUEST_WAIT_FALLBACK_SECONDS))
    )


async def shared_epoch(node_id: int) -> int | None:
    if not needs_shared_bridge_memory():
        return None
    bridge = await node_manager.get_node(node_id)
    if bridge is None:
        return None
    state = await bridge.get_lifecycle_state()
    return None if state is None else state.epoch


async def connect_mark(node_id: int) -> ConnectMark:
    try:
        epoch = await shared_epoch(node_id)
    except Exception as exc:
        logger.warning(f"Could not read the shared lifecycle state of node {node_id}: {describe_error(exc)}")
        epoch = None
    return ConnectMark(connect_generation(node_id), epoch)


def remember_connect_mark(mark: ConnectMark) -> None:
    task = asyncio.current_task()
    if task is not None:
        _marks[task] = mark


async def _answers(node) -> bool:
    bridge = await node_manager.get_node(node.id)
    if bridge is None:
        return False
    try:
        return await bridge.info(timeout=_seconds(node)) is not None
    except Exception as exc:
        logger.debug(
            f'Node "{getattr(node, "name", node.id)}" did not answer before its connect: {describe_error(exc)}'
        )
        return False


class ConnectLanes:
    def __init__(self, slots: int):
        self._slots = asyncio.Semaphore(slots)
        self._unanswered = asyncio.Semaphore(max(1, slots // 2))

    @contextlib.asynccontextmanager
    async def admit(self, node):
        if await _answers(node):
            async with self._slots:
                yield
            return
        async with self._unanswered, self._slots:
            yield


def unit_outcome(node_id: int, unit: asyncio.Task) -> dict | None:
    if unit.cancelled():
        return None
    error = unit.exception()
    if error is not None:
        logger.error(f"Connecting node {node_id} failed: {describe_error(error)}")
        return None
    return unit.result()


async def _still_latest(node_id: int, unit: asyncio.Task) -> bool:
    if is_retiring(node_id) or _another_connect_pending(node_id, unit):
        return False
    mark = _marks.get(unit)
    if mark is None:
        return True
    if connect_generation(node_id) != mark.generation:
        return False
    if mark.epoch is None:
        return True
    try:
        return await shared_epoch(node_id) == mark.epoch
    except Exception as exc:
        logger.warning(f"Could not read the shared lifecycle state of node {node_id}: {describe_error(exc)}")
        return False


async def _finish(node_id: int, unit: asyncio.Task, write: LateWrite) -> None:
    await asyncio.wait({unit})
    result = unit_outcome(node_id, unit)
    if not result:
        return
    written = False
    async with _connect_lock(node_id):
        if not await _still_latest(node_id, unit):
            logger.info(f"Not recording the late connect result of node {node_id}: a newer connect or removal won")
            return
        written = await write(node_id, result)
        bump_connect_generation(node_id)
    if written and needs_shared_bridge_memory():
        await publish_node_sync("connect", node_id)


def _hand_off(node_id: int, unit: asyncio.Task, write: LateWrite) -> None:
    async def finish() -> None:
        try:
            await _finish(node_id, unit, write)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"Could not record the late connect result of node {node_id}: {describe_error(exc)}")

    finisher = asyncio.create_task(finish(), name=f"connect-late-{node_id}")
    _background[finisher] = (node_id, unit)
    finisher.add_done_callback(lambda done: _background.pop(done, None))


async def settle(units: dict[int, asyncio.Task], wait: float, write: LateWrite) -> set[int]:
    try:
        pending = [unit for unit in units.values() if not unit.done()]
        if pending and wait > 0:
            await asyncio.wait(pending, timeout=wait)
    finally:
        for node_id, unit in units.items():
            if not unit.done():
                _hand_off(node_id, unit, write)
    return {node_id for node_id, unit in units.items() if unit.done()}


def connecting_in_background() -> set[int]:
    return {node_id for node_id, _ in _background.values()}


async def cancel_background_connects() -> None:
    pairs = [(finisher, unit) for finisher, (_, unit) in _background.items()]
    _background.clear()
    for finisher, unit in pairs:
        unit.cancel()
        finisher.cancel()
    tasks = [task for pair in pairs for task in pair]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
