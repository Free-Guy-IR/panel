import asyncio

from app import on_shutdown, on_startup
from app.fork.node_removals import drain_node_removals, logger
from config import runtime_settings

DRAIN_INTERVAL_SECONDS = 10.0
DRAIN_TIMEOUT_SECONDS = 60.0

_drain_tasks: list[asyncio.Task] = []


async def _drain_forever() -> None:
    while True:
        try:
            async with asyncio.timeout(DRAIN_TIMEOUT_SECONDS):
                await drain_node_removals()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"pending node removals were not delivered this round and stay recorded: {exc}")
        await asyncio.sleep(DRAIN_INTERVAL_SECONDS)


@on_startup
async def start_node_removal_drain():
    if not runtime_settings.role.runs_node:
        return
    _drain_tasks.append(asyncio.create_task(_drain_forever(), name="node_removal_drain"))


@on_shutdown
async def stop_node_removal_drain():
    running = list(_drain_tasks)
    _drain_tasks.clear()
    for task in running:
        task.cancel()
    if running:
        await asyncio.gather(*running, return_exceptions=True)
