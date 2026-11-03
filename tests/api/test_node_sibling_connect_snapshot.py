import asyncio

from app.db.models import NodeStatus
from app.node.manager_sync import handle_node_message
from app.operation import OperatorType
from app.operation.node import NodeOperation
from tests.api import TestSession, test_node_connect_stale_core as stale_core
from tests.api.helpers import create_core, unique_name
from tests.api.test_bulk_delete_entities import delete_core_if_present, delete_db_node
from tests.api.test_core_delete_restarts_its_nodes import make_node
from tests.api.test_node_connect_stale_core import assert_node_ends_on_current_core, move_node_to_core

gated_connects = stale_core.gated_connects


def test_a_sibling_attach_that_read_the_node_before_an_edit_never_pushes_the_old_core_after_the_edit_restart(
    access_token, gated_connects
):
    old_core = create_core(access_token, name=unique_name("sibling_old"))
    new_core = create_core(access_token, name=unique_name("sibling_new"))
    node = make_node(old_core["id"], node_status=NodeStatus.error)
    sessions: dict[int, str] = {}
    gated_connects.monkeypatch.setattr(
        "app.operation.node.get_node_by_id", gated_connects.tagging_loader(lambda db: sessions[id(db)])
    )
    gated_connects.monkeypatch.setattr("app.node.manager_sync.GetDB", TestSession)
    gated_connects.monkeypatch.setattr(
        "app.node.manager_sync.get_node_by_id", gated_connects.tagging_loader(lambda db: "S")
    )

    async def scenario():
        snapshot_taken, release = asyncio.Event(), asyncio.Event()
        gated_connects.held_callers["S"] = (snapshot_taken, release)
        operation = NodeOperation(operator_type=OperatorType.API)

        sibling = asyncio.create_task(
            handle_node_message({"action": "connect", "node_id": node["id"], "origin": "another-worker"})
        )
        gate = asyncio.create_task(snapshot_taken.wait())
        await asyncio.wait({sibling, gate}, return_when=asyncio.FIRST_COMPLETED)
        if sibling.done():
            gate.cancel()
            sibling.result()
        assert snapshot_taken.is_set(), "the sibling never reached its register step"

        await move_node_to_core(node["id"], new_core["id"])
        async with TestSession() as db2:
            sessions[id(db2)] = "C2"
            await operation._connect_single_node_local(db2, node["id"], force_start=True)

        release.set()
        await sibling

    try:
        asyncio.run(asyncio.wait_for(scenario(), 60))

        assert [push.caller for push in gated_connects.pushes if push.caller == "C2"] == ["C2"]
        assert_node_ends_on_current_core(gated_connects.pushes, node["id"], old_core["id"], new_core["id"])
    finally:
        delete_db_node(node["id"])
        delete_core_if_present(access_token, old_core["id"])
        delete_core_if_present(access_token, new_core["id"])
