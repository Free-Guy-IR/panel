import asyncio
import json
import time
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiorwlock import RWLock
from sqlalchemy import inspect, select, text

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
from tests.api import GetTestDB, TestSession
from tests.api.helpers import unique_name
from tests.test_node_connect_stale_snapshot import Remote, _ca_pem, _core, _model_class

REQUEST_BOUND = 6.0
SETTLE = 12.0
ADMIN = AdminDetails(username="nh-background")


class GatedRemote(Remote):
    def __init__(self):
        super().__init__()
        self.gate = asyncio.Event()
        self.gate.set()
        self.started_with: list[list[str]] = []

    async def rpc(self, name: str, request):
        if name == "Start":
            await self.gate.wait()
            self.started_with.append(json.loads(request.config)["tags"])
        return await super().rpc(name, request)


class Panel:
    def __init__(self, monkeypatch: pytest.MonkeyPatch):
        self.monkeypatch = monkeypatch
        self.remote = GatedRemote()
        self.tags = [unique_name("bg_a")]
        self.real_create_node = app_node.create_node
        self.operation = NodeOperation(operator_type=OperatorType.API)
        self.node_id = 0

    def create_node(self, **kwargs):
        real_class = type(self.real_create_node(**kwargs))
        kwargs.pop("connection")
        return _model_class(real_class)(self.remote, **kwargs)

    async def __aenter__(self):
        patch = self.monkeypatch
        patch.setattr(node_manager, "_nodes", {})
        patch.setattr(node_manager, "_node_signatures", {})
        patch.setattr(node_manager, "_user_sync_locks", {})
        patch.setattr(node_manager, "_lock", RWLock(fast=True))
        patch.setattr(node_extras, "_CONNECT_LOCKS", {})
        patch.setattr(app_node, "create_node", self.create_node)
        patch.setattr(core_manager, "get_cores", AsyncMock(side_effect=self._cores))
        patch.setattr(node_operation, "GetDB", GetTestDB)
        patch.setattr(NodeOperation, "_in_flight_connects", set())
        patch.setattr(NodeOperation, "_pending_user_sync", set())
        for name in ("connect_node", "error_node", "modify_node", "remove_node"):
            patch.setattr(node_operation.notification, name, AsyncMock())
        from app.fork.operation import node_retirement

        patch.setattr(node_retirement, "GetDB", GetTestDB)
        patch.setattr(node_retirement, "needs_shared_bridge_memory", lambda: False)
        patch.setattr(node_retirement, "publish_node_sync", AsyncMock())
        patch.setattr(node_operation, "publish_node_sync", AsyncMock())
        async with TestSession() as session:
            row = Node(
                name=unique_name("bg_node"),
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
            self.node_id = row.id
        pg_node = await node_manager.update_node(await self.row())
        await pg_node.start(config=json.dumps({"tags": self.tags}), backend_type=0, users=[])
        self.remote.started_with.clear()
        return self

    async def __aexit__(self, *exc_info):
        self.remote.gate.set()
        await asyncio.sleep(0.2)
        await node_manager.remove_node(self.node_id, remote_stop=False)
        async with TestSession() as session:
            row = await session.get(Node, self.node_id)
            if row is not None:
                await session.delete(row)
                await session.commit()

    async def _cores(self, ids):
        return {core_id: _core(CoreType.xray, list(self.tags)) for core_id in ids}

    async def row(self) -> Node | None:
        async with TestSession() as session:
            return (await session.execute(select(Node).where(Node.id == self.node_id))).scalar_one_or_none()

    async def restart(self, tags: list[str]) -> float:
        self.tags = tags
        started = time.monotonic()
        async with TestSession() as db:
            try:
                await asyncio.wait_for(self.operation.restart_node(db, self.node_id, ADMIN), timeout=SETTLE)
            except TimeoutError:
                return float("inf")
        return time.monotonic() - started

    async def disable(self) -> float:
        started = time.monotonic()
        async with TestSession() as db:
            try:
                await asyncio.wait_for(
                    self.operation.modify_node(db, self.node_id, NodeModify(status=NodeStatus.disabled), ADMIN),
                    timeout=SETTLE,
                )
            except TimeoutError:
                return float("inf")
        return time.monotonic() - started

    async def delete(self) -> float:
        started = time.monotonic()
        async with TestSession() as db:
            try:
                await asyncio.wait_for(self.operation.remove_node(db, self.node_id, ADMIN), timeout=SETTLE)
            except TimeoutError:
                return float("inf")
        return time.monotonic() - started

    async def owed_stops(self) -> list[str]:
        async with TestSession() as session:
            present = await session.run_sync(lambda sync: inspect(sync.connection()).has_table("node_retirements"))
            if not present:
                return []
            query = text("SELECT reason FROM node_retirements WHERE node_id = :node_id")
            return [reason for (reason,) in (await session.execute(query, {"node_id": self.node_id})).all()]

    async def settle(self, predicate) -> bool:
        deadline = time.monotonic() + SETTLE
        while time.monotonic() < deadline:
            if await predicate():
                return True
            await asyncio.sleep(0.05)
        return await predicate()


@pytest.mark.parametrize("gate_opens", ["before_second", "after_second"])
async def test_the_last_core_save_wins_when_an_earlier_restart_finishes_late(monkeypatch, gate_opens):
    async with Panel(monkeypatch) as panel:
        first, second = [unique_name("bg_b")], [unique_name("bg_c")]
        panel.remote.gate.clear()

        first_took = await panel.restart(first)
        if gate_opens == "before_second":
            panel.remote.gate.set()
        second_took = await panel.restart(second)
        panel.remote.gate.set()

        async def last_config_applied():
            row = await panel.row()
            return (
                panel.remote.started_with[-1:] == [second]
                and panel.remote.running
                and row.status == NodeStatus.connected
            )

        assert first_took < REQUEST_BOUND, f"the first restart waited {first_took:.1f}s for a Start that was held"
        assert second_took < REQUEST_BOUND, f"the second restart waited {second_took:.1f}s behind the first"
        assert await panel.settle(last_config_applied), (
            f"the node did not end on the last saved config: starts={panel.remote.started_with}, "
            f"status={(await panel.row()).status}"
        )
        assert panel.remote.started_with[0] in (first, second)


async def test_disabling_a_node_whose_restart_is_still_running_is_not_overwritten_by_the_late_result(monkeypatch):
    async with Panel(monkeypatch) as panel:
        panel.remote.gate.clear()
        restart_took = await panel.restart([unique_name("bg_d")])

        disable_took = await panel.disable()
        panel.remote.gate.set()
        await asyncio.sleep(0.5)

        row = await panel.row()
        assert restart_took < REQUEST_BOUND, f"the restart waited {restart_took:.1f}s for a held Start"
        assert disable_took < REQUEST_BOUND, f"disabling waited {disable_took:.1f}s behind the held restart"
        assert row.status == NodeStatus.disabled, f"a late restart result overwrote disabled with {row.status}"
        assert await node_manager.get_node(panel.node_id) is None, "the disabled node is still managed"
        assert await panel.settle(lambda: _not_running(panel.remote)), "the disabled node's core kept running"


async def test_deleting_a_node_whose_restart_is_still_running_withdraws_it_and_rechecks_the_stop(monkeypatch):
    async with Panel(monkeypatch) as panel:
        panel.remote.gate.clear()
        restart_took = await panel.restart([unique_name("bg_e")])

        delete_took = await panel.delete()
        panel.remote.gate.set()
        await asyncio.sleep(0.5)

        assert restart_took < REQUEST_BOUND, f"the restart waited {restart_took:.1f}s for a held Start"
        assert delete_took < REQUEST_BOUND, f"the delete waited {delete_took:.1f}s behind the held restart"
        assert await panel.row() is None, "the node row survived the delete"
        assert await node_manager.get_node(panel.node_id) is None, "a late restart registered the deleted node again"
        assert not panel.remote.running, "the withdrawn restart started the deleted node's core"
        assert await panel.owed_stops() == ["settle"], (
            "a delete that withdrew a running restart must recheck the Stop later, because a Start already sent "
            f"can still land: {await panel.owed_stops()}"
        )


async def _not_running(remote: GatedRemote) -> bool:
    return not remote.running
