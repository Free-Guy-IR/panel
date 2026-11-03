import asyncio

from sqlalchemy import select

from app.db.crud.node import get_node_by_id
from app.db.models import Node, NodeStatus
from app.operation import OperatorType
from app.operation.node import NodeOperation
from tests.api import TestSession, test_node_connect_stale_core as stale_core
from tests.api.helpers import create_core, unique_name
from tests.api.test_bulk_delete_entities import delete_core_if_present, delete_db_node
from tests.api.test_core_delete_restarts_its_nodes import make_node
from tests.api.test_node_connect_stale_core import assert_node_ends_on_current_core, move_node_to_core

gated_connects = stale_core.gated_connects


def run_edit_between_read_and_connect(access_token, gated_connects, prefix, read_before, connect_after):
    old_core = create_core(access_token, name=unique_name(f"{prefix}_old"))
    new_core = create_core(access_token, name=unique_name(f"{prefix}_new"))
    node = make_node(old_core["id"], node_status=NodeStatus.error)
    sessions: dict[int, str] = {}
    gated_connects.monkeypatch.setattr(
        "app.operation.node.get_node_by_id", gated_connects.tagging_loader(lambda db: sessions[id(db)])
    )

    async def scenario():
        operation = NodeOperation(operator_type=OperatorType.API)
        async with TestSession() as db1:
            sessions[id(db1)] = "C1"
            read = await read_before(db1, node["id"])

            await move_node_to_core(node["id"], new_core["id"])
            async with TestSession() as db2:
                sessions[id(db2)] = "C2"
                await operation._connect_single_node_local(db2, node["id"], force_start=True)

            await connect_after(operation, db1, node["id"], read)

    try:
        asyncio.run(scenario())

        assert [push.caller for push in gated_connects.pushes if push.caller == "C2"] == ["C2"]
        assert_node_ends_on_current_core(gated_connects.pushes, node["id"], old_core["id"], new_core["id"])
    finally:
        delete_db_node(node["id"])
        delete_core_if_present(access_token, old_core["id"])
        delete_core_if_present(access_token, new_core["id"])


def test_a_connect_on_a_session_whose_read_view_predates_an_edit_never_pushes_the_old_core_after_the_edit_restart(
    access_token, gated_connects
):
    async def read_a_column_only(db, node_id):
        return (await db.execute(select(Node.core_config_id).where(Node.id == node_id))).scalar_one()

    async def connect_on_the_same_transaction(operation, db, node_id, _read):
        await operation._connect_single_node_local(db, node_id, force_start=True)

    run_edit_between_read_and_connect(
        access_token, gated_connects, "rr_view", read_a_column_only, connect_on_the_same_transaction
    )


def test_a_connect_on_a_session_that_already_holds_the_node_never_pushes_the_old_core_after_the_edit_restart(
    access_token, gated_connects
):
    async def load_the_node(db, node_id):
        node = await get_node_by_id(db, node_id, load_usage_logs=False)
        await db.commit()
        return node

    async def connect_on_a_new_transaction(operation, db, node_id, _read):
        await operation._connect_single_node_local(db, node_id, force_start=True)

    run_edit_between_read_and_connect(
        access_token, gated_connects, "rr_identity", load_the_node, connect_on_a_new_transaction
    )


def test_a_bulk_connect_of_nodes_listed_before_an_edit_never_pushes_the_old_core_after_the_edit_restart(
    access_token, gated_connects
):
    async def list_the_node(db, node_id):
        node = await get_node_by_id(db, node_id, load_usage_logs=False)
        gated_connects.current_caller[id(node)] = "C1"
        return node

    async def bulk_connect_the_listed_node(operation, db, _node_id, listed):
        await operation._connect_nodes_bulk_local(db, [listed], force_start=True)

    run_edit_between_read_and_connect(
        access_token, gated_connects, "rr_bulk", list_the_node, bulk_connect_the_listed_node
    )
