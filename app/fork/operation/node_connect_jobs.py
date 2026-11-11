import asyncio
from collections.abc import Awaitable, Callable

from app.fork.operation.node_extras import _connect_lock

_connects: dict[int, set[asyncio.Task]] = {}
_retiring: dict[int, int] = {}
_withdrawn: set[asyncio.Task] = set()


def _forget(node_id: int, task: asyncio.Task) -> None:
    tasks = _connects.get(node_id)
    if tasks is None:
        return
    tasks.discard(task)
    if not tasks:
        _connects.pop(node_id, None)


def _withdrawn_by_a_retirement(job: asyncio.Task) -> bool:
    withdrawn = job in _withdrawn
    _withdrawn.discard(job)
    return withdrawn and job.cancelled()


def is_retiring(node_id: int) -> bool:
    return node_id in _retiring


async def _locked_connect[Result](node_id: int, work: Callable[[], Awaitable[Result]]) -> Result | None:
    async with _connect_lock(node_id):
        if is_retiring(node_id):
            return None
        return await work()


async def run_withdrawable_connect[Result](node_id: int, work: Callable[[], Awaitable[Result]]) -> Result | None:
    if is_retiring(node_id):
        return None
    job = asyncio.create_task(_locked_connect(node_id, work), name=f"connect-{node_id}")
    _connects.setdefault(node_id, set()).add(job)
    job.add_done_callback(lambda done: _forget(node_id, done))
    current = asyncio.current_task()
    try:
        return await job
    except asyncio.CancelledError:
        if _withdrawn_by_a_retirement(job) and (current is None or not current.cancelling()):
            return None
        raise
    finally:
        _withdrawn.discard(job)


async def withdraw_connects(node_id: int) -> bool:
    _retiring[node_id] = _retiring.get(node_id, 0) + 1
    running = [task for task in _connects.get(node_id, ()) if not task.done()]
    for task in running:
        _withdrawn.add(task)
        task.cancel()
    if running:
        await asyncio.gather(*running, return_exceptions=True)
    return bool(running)


def readmit_connects(node_id: int) -> None:
    holders = _retiring.get(node_id, 0) - 1
    if holders > 0:
        _retiring[node_id] = holders
    else:
        _retiring.pop(node_id, None)
