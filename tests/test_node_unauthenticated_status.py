from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiorwlock import RWLock
from fastapi import HTTPException
from grpclib.const import Status
from grpclib.exceptions import GRPCError
from PasarGuardNodeBridge import NodeAPIError
from sqlalchemy import select

from app.core.manager import core_manager
from app.db.models import Node, NodeConnectionType, NodeStatus
from app.models.core import CoreType
from app.node import node_manager
from app.operation import OperatorType, node as node_operation
from app.operation.node import NodeOperation
from tests.api import TestSession
from tests.api.helpers import unique_name
from tests.test_node_connect_stale_snapshot import _ca_pem, _core

NODE_REFUSAL = "node api key is not configured"


class UnauthenticatedNode:
    def __getattr__(self, rpc):
        async def refuse(request, metadata=None):
            raise GRPCError(Status.UNAUTHENTICATED, NODE_REFUSAL)

        return refuse


@pytest.fixture
def fresh_node_manager(monkeypatch):
    monkeypatch.setattr(node_manager, "_nodes", {})
    monkeypatch.setattr(node_manager, "_node_signatures", {})
    monkeypatch.setattr(node_manager, "_user_sync_locks", {})
    monkeypatch.setattr(node_manager, "_lock", RWLock(fast=True))


async def seed_connected_node() -> Node:
    async with TestSession() as session:
        row = Node(
            name=unique_name("unauthenticated_node"),
            address=f"{unique_name('unauthenticated')}.nodes.test",
            port=1,
            api_port=1,
            server_ca=_ca_pem(),
            api_key=str(uuid4()),
            core_config_id=None,
        )
        row.connection_type = NodeConnectionType.grpc
        session.add(row)
        await session.flush()
        row.status = NodeStatus.connected
        await session.commit()
        return row


async def node_whose_core_refuses_the_panel(monkeypatch):
    row = await seed_connected_node()
    pg_node = await node_manager.update_node(row)
    pg_node._client = UnauthenticatedNode()
    monkeypatch.setattr(core_manager, "get_cores", AsyncMock(return_value={1: _core(CoreType.xray, ["unauth"])}))
    return row, pg_node


async def stored_status(node_id: int) -> NodeStatus:
    async with TestSession() as session:
        return (await session.execute(select(Node.status).where(Node.id == node_id))).scalar_one()


async def answered_status(call) -> int:
    with pytest.raises(HTTPException) as caught:
        await call
    return caught.value.status_code


async def test_the_bridge_maps_a_node_that_refuses_the_panel_to_401(monkeypatch, fresh_node_manager):
    _, pg_node = await node_whose_core_refuses_the_panel(monkeypatch)

    with pytest.raises(NodeAPIError) as caught:
        await pg_node.get_system_stats()

    assert (caught.value.code, caught.value.detail) == (401, NODE_REFUSAL)


QUERIES = {
    "realtime_stats": lambda operation, row: operation.get_node_system_stats(row.id),
    "outbounds_latency": lambda operation, row: operation.get_outbounds_latency(row.id),
}


@pytest.mark.parametrize("query", sorted(QUERIES))
async def test_a_node_that_refuses_the_panel_is_not_answered_to_the_admin_as_the_admins_own_401(
    monkeypatch, fresh_node_manager, query
):
    row, _ = await node_whose_core_refuses_the_panel(monkeypatch)
    operation = NodeOperation(OperatorType.API)

    status = await answered_status(QUERIES[query](operation, row))

    assert status != 401, (
        f"the node answered gRPC UNAUTHENTICATED ({NODE_REFUSAL!r}) and the panel answered the admin's {query} "
        "request with 401, which the dashboard takes as the admin's own expired session and sends them to the login page"
    )
    assert status == 502


async def test_a_user_sync_to_a_node_that_refuses_the_panel_is_not_the_admins_own_401(
    monkeypatch, fresh_node_manager
):
    row, _ = await node_whose_core_refuses_the_panel(monkeypatch)
    monkeypatch.setattr(node_operation.notification, "error_node", AsyncMock())
    operation = NodeOperation(OperatorType.API)

    async with TestSession() as db:
        status = await answered_status(operation._sync_node_users_local(db, row.id, flush_users=False))

    assert status == 502, f"a user sync the node refused with UNAUTHENTICATED answered the admin {status}"
    assert await stored_status(row.id) == NodeStatus.error


async def test_a_worker_that_still_runs_the_old_code_and_relays_a_node_401_is_answered_as_502(monkeypatch):
    class OldWorkerReply:
        is_closed = False

        async def request(self, subject, data, timeout=None):
            return SimpleNamespace(data=b'{"ok": false, "error": "node api key is not configured", "code": 401}')

    async def old_worker():
        return OldWorkerReply()

    monkeypatch.setattr(node_operation.node_nats_client, "_get_client", old_worker)

    status = await answered_status(NodeOperation(OperatorType.API)._get_node_system_stats_remote(1))

    assert status == 502
