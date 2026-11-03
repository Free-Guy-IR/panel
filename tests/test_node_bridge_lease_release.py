import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.node.bridge import QueuedGrpcNode, QueuedRestNode


def _node(cls):
    node = object.__new__(cls)
    node._lifecycle_heartbeat_tasks = {}
    node._lifecycle_coordinator = SimpleNamespace(release=AsyncMock())
    return node


async def _heartbeat_forever():
    await asyncio.Event().wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("cls", [QueuedGrpcNode, QueuedRestNode])
async def test_release_with_an_unstarted_heartbeat_does_not_raise_cancelled(cls):
    node = _node(cls)
    lease = SimpleNamespace(token="t1", epoch=1, operation="start", worker_id="w1")
    heartbeat = asyncio.create_task(_heartbeat_forever())
    node._lifecycle_heartbeat_tasks[lease.token] = heartbeat

    await node._release_lifecycle_lease(lease)

    assert heartbeat.cancelled()
    assert lease.token not in node._lifecycle_heartbeat_tasks
    node._lifecycle_coordinator.release.assert_awaited_once_with(lease)


@pytest.mark.asyncio
async def test_release_keeps_the_callers_own_cancellation():
    node = _node(QueuedGrpcNode)
    lease = SimpleNamespace(token="t2", epoch=1, operation="start", worker_id="w1")
    entered = asyncio.Event()

    async def slow_heartbeat():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.05)

    node._lifecycle_heartbeat_tasks[lease.token] = asyncio.create_task(slow_heartbeat())
    await entered.wait()

    caller = asyncio.create_task(node._release_lifecycle_lease(lease))
    await asyncio.sleep(0.01)
    caller.cancel()

    with pytest.raises(asyncio.CancelledError):
        await caller
