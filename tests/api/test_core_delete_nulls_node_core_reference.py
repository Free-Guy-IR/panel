import asyncio

import pytest

from app.db.crud.core import remove_core_config, remove_cores
from app.db.models import CoreConfig, Node
from app.operation.node import NodeOperation
from app.routers import core as core_router
from tests.api import TestSession
from tests.api.helpers import create_core, unique_name
from tests.api.test_bulk_delete_entities import delete_core_if_present, delete_db_node
from tests.api.test_core_delete_restarts_its_nodes import make_node, stored_node


@pytest.fixture
def restarts(monkeypatch):
    issued = []

    async def connect_nodes_bulk(self, db, nodes, *, force_start=False):
        issued.append((sorted((node.id, node.core_config_id) for node in nodes), force_start))

    monkeypatch.setattr(NodeOperation, "connect_nodes_bulk", connect_nodes_bulk)
    return issued


async def _delete_past_the_guard(core_ids: list[int], node_ids: list[int], bulk: bool):
    async with TestSession() as session:
        loaded = [await session.get(Node, node_id) for node_id in node_ids]
        if bulk:
            await remove_cores(session, core_ids)
        else:
            await remove_core_config(session, await session.get(CoreConfig, core_ids[0]))
        in_session = sorted((node.id, node.core_config_id) for node in loaded)
        await core_router._restart_deleted_core_nodes(session, node_ids)
        return in_session


@pytest.mark.parametrize("bulk", [False, True], ids=["single", "bulk"])
def test_a_core_deleted_under_a_node_nulls_its_core_reference_and_force_restarts_exactly_its_nodes(
    access_token, restarts, bulk
):
    targets = [create_core(access_token, name=unique_name("coredel_null")) for _ in range(2 if bulk else 1)]
    affected = [make_node(target["id"]) for target in targets]
    bystander = make_node(1)
    affected_ids = sorted(node["id"] for node in affected)
    try:
        in_session = asyncio.run(_delete_past_the_guard([target["id"] for target in targets], affected_ids, bulk))

        assert in_session == [(node_id, None) for node_id in affected_ids]
        for node_id in affected_ids:
            assert stored_node(node_id)[0] is None
        assert stored_node(bystander["id"]) == bystander["refs"]
        assert len(restarts) == 1, restarts
        assert restarts == [([(node_id, None) for node_id in affected_ids], True)]
    finally:
        for node in [*affected, bystander]:
            delete_db_node(node["id"])
        for target in targets:
            delete_core_if_present(access_token, target["id"])
