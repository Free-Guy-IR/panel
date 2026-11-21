import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.nats.node_rpc import node_nats_client
from app.node import node_manager, worker as node_worker
from app.operation import OperatorType, node as node_operation
from tests.test_node_connect_stale_snapshot import Harness
from tests.test_node_delete_stop_contract import (
    SessionOfItsOwn,
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


@pytest.mark.parametrize("window", ["after_the_worker_read", "inside_the_registration"])
async def test_a_backend_role_delete_delivered_inside_a_worker_update_node_leaves_no_node_managed(monkeypatch, window):
    quiet_side_effects(monkeypatch)
    worker = node_worker.NodeWorkerService.__new__(node_worker.NodeWorkerService)
    deliveries: list[asyncio.Task] = []

    async def publish(action, payload):
        if action == "remove_node":
            deliveries.append(asyncio.create_task(worker._remove_node(payload)))

    monkeypatch.setattr(node_nats_client, "publish", publish)
    backend = backend_role_operation(monkeypatch, OperatorType.SYSTEM)
    async with Harness(monkeypatch) as h:
        row, db_node = await managed_row(h, monkeypatch, row_backed=True)
        remote = h.remote(db_node)
        remote.lost = {"Stop"}
        deleted: dict[str, object] = {}

        async def delete_and_deliver():
            deleted["refusal"] = await refused_delete(row, bulk=False, operation=backend)
            if deliveries:
                await asyncio.wait(deliveries, timeout=DELIVERY_GRACE_SECONDS)

        rows = node_operation.get_node_by_id.side_effect

        async def worker_read(db, node_id, **kwargs):
            found = await rows(db, node_id, **kwargs)
            if window == "after_the_worker_read" and not deleted:
                await delete_and_deliver()
            return found

        monkeypatch.setattr(node_worker, "GetDB", SessionOfItsOwn)
        monkeypatch.setattr(node_worker, "get_node_by_id", AsyncMock(side_effect=worker_read))

        async def before_register():
            if window == "inside_the_registration" and not deleted:
                await delete_and_deliver()

        h.wrap_update_node(before=before_register)
        await worker._update_node({"node_id": row.id})
        await asyncio.wait_for(asyncio.gather(*deliveries), timeout=DELIVERY_DEADLINE_SECONDS)

        h.db_nodes.pop(row.id, None)
        assert deleted and deleted["refusal"] is None, deleted
        assert deliveries, "the backend role never published remove_node"
        assert not await node_row_exists(row.id)
        assert await node_manager.get_node(row.id) is None, (
            "the node worker handled remove_node while its update_node command was registering the node, and "
            "update_node registered it afterwards: a node object is still managed for a deleted row"
        )
        assert await stops_owed_to(row.address) == 1

        remote.lost = set()
        await drain_owed_stops(monkeypatch, datetime.now(UTC) + timedelta(days=1))
        assert not remote.running, "the owed Stop of the deleted node never stopped its core"
        assert await stops_owed_to(row.address) == 0
