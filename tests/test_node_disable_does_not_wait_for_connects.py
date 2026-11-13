import asyncio
import time
from unittest.mock import AsyncMock
from uuid import uuid4

from aiorwlock import RWLock
from sqlalchemy import select

import app.node as app_node
from app.core.manager import core_manager
from app.db.models import Node, NodeConnectionType, NodeStatus
from app.fork.operation import node_extras
from app.models.admin import AdminDetails
from app.models.core import CoreType
from app.models.node import NodeModify
from app.node import node_manager
from app.operation import OperatorType, node as node_operation
from app.operation.node import NodeOperation
from tests.api.helpers import unique_name
from tests.test_node_connect_stale_snapshot import Remote, _ca_pem, _core, _model_class
from tests.test_node_delete_stop_contract import Session, SessionOfItsOwn

REQUEST_BOUND = 6.0
SETTLE = 12.0
ADMIN = AdminDetails(username="u2-disable")


class HeldStart(Remote):
    def __init__(self):
        super().__init__()
        self.gate = asyncio.Event()
        self.held = 0

    async def rpc(self, name: str, request):
        if name == "Start":
            self.held += 1
            await self.gate.wait()
        return await super().rpc(name, request)


async def test_disabling_a_node_does_not_wait_for_a_connect_still_running_for_it(monkeypatch):
    remote = HeldStart()
    tags = [unique_name("disable_a")]
    real_create_node = app_node.create_node

    def create_node(**kwargs):
        real_class = type(real_create_node(**kwargs))
        kwargs.pop("connection")
        return _model_class(real_class)(remote, **kwargs)

    async def cores(ids):
        return {core_id: _core(CoreType.xray, list(tags)) for core_id in ids}

    monkeypatch.setattr(node_manager, "_nodes", {})
    monkeypatch.setattr(node_manager, "_node_signatures", {})
    monkeypatch.setattr(node_manager, "_user_sync_locks", {})
    monkeypatch.setattr(node_manager, "_lock", RWLock(fast=True))
    monkeypatch.setattr(node_extras, "_CONNECT_LOCKS", {})
    monkeypatch.setattr(app_node, "create_node", create_node)
    monkeypatch.setattr(core_manager, "get_cores", AsyncMock(side_effect=cores))
    monkeypatch.setattr(node_operation, "GetDB", SessionOfItsOwn)
    monkeypatch.setattr(node_operation, "needs_shared_bridge_memory", lambda: False)
    monkeypatch.setattr(NodeOperation, "_in_flight_connects", set())
    monkeypatch.setattr(NodeOperation, "_pending_user_sync", set())
    for name in ("connect_node", "error_node", "modify_node", "remove_node"):
        monkeypatch.setattr(node_operation.notification, name, AsyncMock())
    async with Session() as session:
        row = Node(
            name=unique_name("disable_node"),
            address="127.0.0.1",
            port=1,
            api_port=1,
            server_ca=_ca_pem(),
            api_key=str(uuid4()),
            core_config_id=None,
            connection_type=NodeConnectionType.grpc,
            default_timeout=5,
            internal_timeout=3,
            status=NodeStatus.connected,
        )
        session.add(row)
        await session.commit()
        node_id = row.id
    operation = NodeOperation(operator_type=OperatorType.API)

    async def restart():
        async with Session() as db:
            await operation.restart_node(db, node_id, ADMIN)

    running = asyncio.create_task(restart())
    try:
        deadline = time.monotonic() + SETTLE
        while not remote.held and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert remote.held, "the restart never reached its Start"

        started = time.monotonic()
        async with Session() as db:
            try:
                await asyncio.wait_for(
                    operation.modify_node(db, node_id, NodeModify(status=NodeStatus.disabled), ADMIN), timeout=SETTLE
                )
                took = time.monotonic() - started
            except TimeoutError:
                took = float("inf")

        remote.gate.set()
        await asyncio.wait_for(asyncio.gather(running, return_exceptions=True), timeout=SETTLE)
        async with Session() as session:
            stored = (await session.execute(select(Node).where(Node.id == node_id))).scalar_one()

        assert took < REQUEST_BOUND, f"disabling waited {took:.1f}s for the connect still running for the node"
        assert stored.status is NodeStatus.disabled
        assert await node_manager.get_node(node_id) is None, "the disabled node is still managed"
        assert not remote.running, "the disabled node's core kept running"
    finally:
        remote.gate.set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        await node_manager.remove_node(node_id, remote_stop=False)
        async with Session() as session:
            row = await session.get(Node, node_id)
            if row is not None:
                await session.delete(row)
                await session.commit()
