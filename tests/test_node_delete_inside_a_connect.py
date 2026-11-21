import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.jobs import node_checker
from app.nats.node_rpc import node_nats_client
from app.node import node_manager, worker as node_worker
from app.operation import OperatorType, node as node_operation
from app.operation.node import NodeOperation
from tests.test_node_connect_stale_snapshot import Harness, _operation
from tests.test_node_delete_stop_contract import (
    Session,
    backend_role_operation,
    drain_owed_stops,
    managed_row,
    node_row_exists,
    quiet_side_effects,
    refused_delete,
    single_process_bridge_memory,  # noqa: F401
    stops_owed_to,
)

DELIVERY_GRACE_SECONDS = 1.0
DELIVERY_DEADLINE_SECONDS = 10.0


def deliver_removals_to_a_node_worker(monkeypatch) -> list[asyncio.Task]:
    worker = node_worker.NodeWorkerService.__new__(node_worker.NodeWorkerService)
    deliveries: list[asyncio.Task] = []

    async def publish(action, payload):
        if action == "remove_node":
            deliveries.append(asyncio.create_task(worker._remove_node(payload)))

    monkeypatch.setattr(node_nats_client, "publish", publish)
    return deliveries


@pytest.mark.parametrize("window", ["after_the_reread", "before_the_reread"])
@pytest.mark.parametrize("bulk", [False, True], ids=["single", "bulk"])
async def test_a_backend_role_delete_delivered_inside_a_connect_leaves_no_node_managed_and_starts_no_core(
    monkeypatch, window, bulk
):
    quiet_side_effects(monkeypatch)
    monkeypatch.setattr(node_checker, "after_healthy_node_check", AsyncMock())
    monkeypatch.setattr(NodeOperation, "_update_single_node_status", AsyncMock())
    deliveries = deliver_removals_to_a_node_worker(monkeypatch)
    backend = backend_role_operation(monkeypatch, OperatorType.SYSTEM)
    async with Harness(monkeypatch) as h:
        row, db_node = await managed_row(h, monkeypatch, row_backed=True)
        remote = h.remote(db_node)
        remote.lost = {"Stop"}
        await node_manager.remove_node(row.id, remote_stop=False)
        deleted: dict[str, object] = {}
        starts_after_the_delete: list[str] = []
        real_rpc = remote.rpc

        async def rpc(name, request):
            if name == "Start" and deleted:
                starts_after_the_delete.append(name)
            return await real_rpc(name, request)

        remote.rpc = rpc

        async def delete_while_the_connect_holds_its_lock():
            deleted["refusal"] = await refused_delete(row, bulk=False, operation=backend)
            if deliveries:
                await asyncio.wait(deliveries, timeout=DELIVERY_GRACE_SECONDS)

        reads = node_operation.get_node_by_id.side_effect

        async def read(db, node_id, **kwargs):
            if window == "before_the_reread" and not deleted and kwargs.get("fresh_view") is None:
                await delete_while_the_connect_holds_its_lock()
            return await reads(db, node_id, **kwargs)

        monkeypatch.setattr(node_operation, "get_node_by_id", AsyncMock(side_effect=read))

        async def before_register():
            if window == "after_the_reread" and not deleted:
                await delete_while_the_connect_holds_its_lock()

        h.wrap_update_node(before=before_register)
        async with Session() as db:
            if bulk:
                await _operation().connect_nodes_bulk(db, [db_node], force_start=True)
            else:
                await _operation().connect_single_node(db, row.id, force_start=True)
        await asyncio.wait_for(asyncio.gather(*deliveries), timeout=DELIVERY_DEADLINE_SECONDS)

        h.db_nodes.pop(row.id, None)
        assert deleted and deleted["refusal"] is None, deleted
        assert deliveries, "the backend role never published remove_node"
        assert not await node_row_exists(row.id)
        assert await node_manager.get_node(row.id) is None, (
            "the node worker handled remove_node while a connect was registering the node, and the connect "
            "registered it afterwards: a node object is still managed for a deleted row"
        )
        assert starts_after_the_delete == [], "a connect sent Start to the core of a node whose row was deleted"
        assert await stops_owed_to(row.address) == 1

        remote.lost = set()
        await drain_owed_stops(monkeypatch, datetime.now(UTC) + timedelta(days=1))
        assert not remote.running, "the owed Stop of the deleted node never stopped its core"
        assert await node_manager.get_node(row.id) is None
        assert await stops_owed_to(row.address) == 0


async def test_a_remove_node_delivered_while_a_start_hangs_withdraws_that_connect_instead_of_waiting_behind_it(
    monkeypatch,
):
    quiet_side_effects(monkeypatch)
    monkeypatch.setattr(node_checker, "after_healthy_node_check", AsyncMock())
    monkeypatch.setattr(NodeOperation, "_update_single_node_status", AsyncMock())
    deliveries = deliver_removals_to_a_node_worker(monkeypatch)
    backend = backend_role_operation(monkeypatch, OperatorType.SYSTEM)
    async with Harness(monkeypatch) as h:
        row, db_node = await managed_row(h, monkeypatch, row_backed=True)
        remote = h.remote(db_node)
        start_reached = asyncio.Event()
        start_released = asyncio.Event()

        async def hang_the_start():
            start_reached.set()
            await start_released.wait()

        remote.on("Start", hang_the_start)
        delivered: set[asyncio.Task] = set()
        withdrawn: set[asyncio.Task] = set()
        async with Session() as db:
            connecting = asyncio.create_task(_operation().connect_single_node(db, row.id, force_start=True))
            try:
                await asyncio.wait_for(start_reached.wait(), timeout=DELIVERY_DEADLINE_SECONDS)
                refusal = await refused_delete(row, bulk=False, operation=backend)
                if deliveries:
                    delivered, _ = await asyncio.wait(deliveries, timeout=DELIVERY_GRACE_SECONDS)
                withdrawn, _ = await asyncio.wait([connecting], timeout=DELIVERY_GRACE_SECONDS)
            finally:
                start_released.set()
                await asyncio.wait_for(
                    asyncio.gather(connecting, *deliveries, return_exceptions=True),
                    timeout=DELIVERY_DEADLINE_SECONDS,
                )

        h.db_nodes.pop(row.id, None)
        assert refusal is None, refusal
        assert deliveries, "the backend role never published remove_node"
        assert delivered == set(deliveries), "remove_node waited behind a connect whose Start hangs"
        assert connecting in withdrawn, "the connect of the deleted node kept running after remove_node arrived"
        assert not remote.running, "the withdrawn Start of the deleted node still started its core"
        assert await node_manager.get_node(row.id) is None, "a node object is still managed for a deleted row"
        assert await stops_owed_to(row.address) == 1
