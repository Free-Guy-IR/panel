import asyncio
import gc
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.fork.operation import node_extras
from app.fork.operation.node_extras import (
    connect_snapshot,
    note_ambiguous_start,
    note_connect_push,
    resync_while_a_start_may_land,
    with_connect_snapshot,
)
from app.node import manager_sync, worker as node_worker
from app.operation import OperatorType, node as node_operation
from app.operation.node import NodeOperation

NODE_ID = 987_654
REMOVAL_PATHS = ["local", "nats_worker", "sibling_sync"]


def reused_node():
    return SimpleNamespace(id=NODE_ID, name="reused-id-node", default_timeout=1)


@pytest.fixture
def removal(monkeypatch):
    stub_manager = SimpleNamespace(remove_node=AsyncMock())
    monkeypatch.setattr(node_operation, "node_manager", stub_manager)
    monkeypatch.setattr(node_operation, "clear_bridge_memory_for_node", AsyncMock())
    monkeypatch.setattr(node_operation, "clear_local_user_queue", AsyncMock())
    monkeypatch.setattr(node_worker, "node_manager", stub_manager)
    monkeypatch.setattr(manager_sync, "node_manager", stub_manager)
    monkeypatch.setattr(manager_sync, "clear_bridge_memory_for_node", AsyncMock())

    async def remove(path: str) -> None:
        if path == "local":
            await NodeOperation(operator_type=OperatorType.SYSTEM)._remove_node_local(NODE_ID)
        elif path == "nats_worker":
            await node_worker.NodeWorkerService.__new__(node_worker.NodeWorkerService)._remove_node(
                {"node_id": NODE_ID}
            )
        else:
            await manager_sync.handle_node_message({"action": "remove", "node_id": NODE_ID, "origin": "other-worker"})
        stub_manager.remove_node.assert_awaited()

    yield remove
    node_extras._pushed_snapshots.pop(NODE_ID, None)
    node_extras._ambiguous_starts.pop(NODE_ID, None)


@pytest.fixture
def connects(monkeypatch):
    reached: list[int] = []

    async def inner(db_node, *args):
        reached.append(db_node.id)

    monkeypatch.setattr(NodeOperation, "_connect_node_inner", staticmethod(inner))
    return reached


async def connect_with(snapshot: int) -> None:
    await with_connect_snapshot(snapshot, NodeOperation.connect_node(reused_node(), object(), []))


@pytest.mark.parametrize("path", REMOVAL_PATHS)
async def test_every_removal_path_lets_the_first_connect_of_a_node_reusing_the_id_through(path, removal, connects):
    drawn_before_the_old_push = connect_snapshot()
    note_connect_push(NODE_ID, connect_snapshot())

    await removal(path)
    await connect_with(drawn_before_the_old_push)

    assert connects == [NODE_ID]
    assert NODE_ID not in node_extras._pushed_snapshots


@pytest.mark.parametrize("path", REMOVAL_PATHS)
async def test_every_removal_path_ends_the_removed_nodes_ambiguous_start_window(path, removal):
    note_ambiguous_start(reused_node())

    await removal(path)
    resync = AsyncMock(return_value=True)
    await resync_while_a_start_may_land(reused_node(), resync)

    assert NODE_ID not in node_extras._ambiguous_starts
    resync.assert_not_awaited()


@pytest.mark.parametrize("path", REMOVAL_PATHS)
async def test_no_connect_lock_outlives_its_last_user_after_a_removal(path, removal, connects):
    await connect_with(connect_snapshot())
    await removal(path)
    gc.collect()

    assert connects == [NODE_ID]
    assert NODE_ID not in node_extras._CONNECT_LOCKS


@pytest.mark.parametrize("path", REMOVAL_PATHS)
async def test_a_removal_between_a_held_connect_and_its_waiters_never_runs_two_connects_of_one_id_at_once(
    path, removal, monkeypatch
):
    running = 0
    overlaps: list[int] = []
    locks_seen: list[object] = []
    finished: list[str] = []
    holding = asyncio.Event()
    release = asyncio.Event()

    async def inner(db_node, core, *args):
        nonlocal running
        running += 1
        overlaps.append(running)
        locks_seen.append(node_extras._CONNECT_LOCKS.get(db_node.id))
        if core == "holder":
            holding.set()
            await release.wait()
        else:
            await asyncio.sleep(0)
        running -= 1
        finished.append(core)

    monkeypatch.setattr(NodeOperation, "_connect_node_inner", staticmethod(inner))

    def connect(name: str) -> asyncio.Task:
        return asyncio.create_task(
            with_connect_snapshot(connect_snapshot(), NodeOperation.connect_node(reused_node(), name, []))
        )

    holder = connect("holder")
    await asyncio.wait_for(holding.wait(), 5)
    waiter = connect("waiter")
    await asyncio.sleep(0)

    await removal(path)
    gc.collect()
    late = connect("after-removal")
    for _ in range(5):
        await asyncio.sleep(0)

    assert overlaps == [1]
    release.set()
    await asyncio.wait_for(asyncio.gather(holder, waiter, late), 5)

    assert sorted(finished) == ["after-removal", "holder", "waiter"]
    assert overlaps == [1, 1, 1]
    assert locks_seen[0] is not None
    assert all(lock is locks_seen[0] for lock in locks_seen)
