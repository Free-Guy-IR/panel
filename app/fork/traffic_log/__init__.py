import asyncio
import contextlib
from collections.abc import AsyncIterator

from app.fork.traffic_log.collector import DETACHED, TAP_QUEUE_SIZE, VIEWER_HANDOFF_TIMEOUT, collector
from app.utils.logger import get_logger

logger = get_logger("traffic-log")


def fork_log_stream(node_id: int, node):
    return lambda **kwargs: viewer_stream(node_id, node, kwargs)


@contextlib.asynccontextmanager
async def viewer_stream(node_id: int, node, kwargs: dict) -> AsyncIterator[asyncio.Queue]:
    outbox: asyncio.Queue = asyncio.Queue(maxsize=TAP_QUEUE_SIZE)
    pump = asyncio.create_task(_pump(node_id, node, kwargs, outbox), name=f"traffic-log-viewer-{node_id}")
    try:
        yield outbox
    finally:
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await pump


async def _pump(node_id: int, node, kwargs: dict, outbox: asyncio.Queue) -> None:
    async with collector.tap(node_id) as tapped:
        while True:
            try:
                item = await tapped.get()
                if item is DETACHED:
                    await collector.ensure_direct_pump(node_id, node, kwargs)
                    continue
                await _hand_over(node_id, outbox, item)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(f"the traffic log viewer feed for node {node_id} hit an error and will keep reading")
                await asyncio.sleep(VIEWER_HANDOFF_TIMEOUT)


async def _hand_over(node_id: int, outbox: asyncio.Queue, item) -> None:
    try:
        await asyncio.wait_for(outbox.put(item), timeout=VIEWER_HANDOFF_TIMEOUT)
    except TimeoutError:
        collector.note_viewer_drop(node_id)


__all__ = ["collector", "fork_log_stream", "viewer_stream"]
