import asyncio
from types import SimpleNamespace

import pytest

from app.node import node_manager
from app.operation.node import NodeOperation
from tests.test_node_connect_stale_snapshot import Harness
from tests.test_node_delete_stop_contract import delete, managed_row, node_row_exists, quiet_side_effects


@pytest.mark.parametrize("change", ["connection", "metadata"])
async def test_an_edit_that_lands_after_a_delete_never_registers_the_deleted_node(monkeypatch, change):
    quiet_side_effects(monkeypatch)
    async with Harness(monkeypatch) as h:
        row, db_node = await managed_row(h, monkeypatch, row_backed=True)
        changed = {"port": db_node.port + 1} if change == "connection" else {"usage_coefficient": 2.0}
        edited = SimpleNamespace(**{**vars(db_node), **changed})
        gate = asyncio.Event()
        real_update = NodeOperation._update_node_local

        async def edit_applied_late(self, node):
            await gate.wait()
            return await real_update(self, node)

        monkeypatch.setattr(NodeOperation, "_update_node_local", edit_applied_late)
        operation = NodeOperation.__new__(NodeOperation)
        edit = asyncio.create_task(operation._update_node_local(edited))
        await asyncio.sleep(0)

        await delete(row, bulk=False)
        assert not await node_row_exists(row.id)
        gate.set()
        await asyncio.gather(edit, return_exceptions=True)

        registered = await node_manager.get_node(row.id)
        h.db_nodes.pop(row.id, None)
        if registered is not None:
            await node_manager.remove_node(row.id, remote_stop=False)
        assert registered is None, (
            f"a {change} edit committed before the delete and applied after it registered the deleted node again"
        )
