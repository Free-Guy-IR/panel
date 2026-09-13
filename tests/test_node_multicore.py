from types import SimpleNamespace

import pytest

from app.models.core import CoreType
from app.operation.node import _MULTI_INSTANCE_BACKENDS, NodeOperation


def node(core_id=None, extra=None):
    return SimpleNamespace(id=7, name="de-1", core_config_id=core_id, additional_core_config_ids=extra)


def test_no_additional_cores_behaves_exactly_as_before():
    assert NodeOperation._node_core_ids(node(core_id=2, extra=None)) == [2]
    assert NodeOperation._node_core_ids(node(core_id=2, extra=[])) == [2]


def test_missing_primary_falls_back_to_the_default_core():
    assert NodeOperation._node_core_ids(node(core_id=None)) == [1]
    assert NodeOperation._node_core_ids(node(core_id=None, extra=[4])) == [1, 4]


def test_primary_comes_first_and_order_is_kept():
    assert NodeOperation._node_core_ids(node(core_id=3, extra=[5, 9])) == [3, 5, 9]


def test_duplicates_are_collapsed():
    assert NodeOperation._node_core_ids(node(core_id=3, extra=[5, 3, 5])) == [3, 5]


def test_extra_cores_exclude_the_primary():
    cores = {2: "core-2", 5: "core-5"}
    users = {2: ["u2"], 5: ["u5"]}
    extras = NodeOperation._extra_cores_for(node(core_id=2, extra=[5]), cores, users)
    assert extras == [(5, "core-5", ["u5"])]


def test_single_core_node_has_no_extra_cores():
    assert NodeOperation._extra_cores_for(node(core_id=2), {2: "core-2"}, {2: ["u"]}) == []


def test_xray_is_not_a_multi_instance_backend():
    assert CoreType.xray not in _MULTI_INSTANCE_BACKENDS


def test_every_other_core_type_can_run_alongside():
    for core_type in (CoreType.wg, CoreType.singbox, CoreType.openvpn, CoreType.mtproto, CoreType.l2tp):
        assert core_type in _MULTI_INSTANCE_BACKENDS


@pytest.mark.asyncio
async def test_add_extra_cores_is_a_noop_without_extras():
    assert await NodeOperation._add_extra_cores(object(), node(core_id=2), None) == ""
    assert await NodeOperation._add_extra_cores(object(), node(core_id=2), []) == ""


@pytest.mark.asyncio
async def test_a_failing_extra_core_is_reported_but_does_not_raise():
    class Node:
        async def list_backends(self):
            return None

        async def add_backend(self, **kwargs):
            raise RuntimeError("boom")

        async def node_version(self):
            return "0.7.4"

    core = SimpleNamespace(type=CoreType.singbox, to_str=lambda: "{}")
    message = await NodeOperation._add_extra_cores(Node(), node(core_id=1, extra=[2]), [(2, core, [])])
    assert "core 2" in message and "boom" in message


@pytest.mark.asyncio
async def test_an_already_running_backend_is_not_added_twice():
    import PasarGuardNodeBridge.common.service_pb2 as service

    added = []

    class Node:
        async def list_backends(self):
            return SimpleNamespace(types=[service.BackendType.SING_BOX])

        async def add_backend(self, **kwargs):
            added.append(kwargs)

    core = SimpleNamespace(type=CoreType.singbox, to_str=lambda: "{}")
    message = await NodeOperation._add_extra_cores(Node(), node(core_id=1, extra=[2]), [(2, core, [])])
    assert message == ""
    assert added == []


@pytest.mark.asyncio
async def test_a_missing_core_is_reported_not_skipped_silently():
    class Node:
        async def list_backends(self):
            return None

    message = await NodeOperation._add_extra_cores(Node(), node(core_id=1, extra=[42]), [(42, None, [])])
    assert "core 42 not found" in message


@pytest.mark.asyncio
async def test_a_running_backend_is_replaced_when_a_restart_is_requested():
    import PasarGuardNodeBridge.common.service_pb2 as service

    calls = []

    class Node:
        async def list_backends(self):
            return SimpleNamespace(types=[service.BackendType.SING_BOX])

        async def remove_backend(self, backend_type, **_):
            calls.append(("remove", backend_type))

        async def add_backend(self, backend_type, **_):
            calls.append(("add", backend_type))

    core = SimpleNamespace(type=CoreType.singbox, to_str=lambda: "{}")
    message = await NodeOperation._add_extra_cores(
        Node(), node(core_id=1, extra=[2]), [(2, core, [])], restart_running=True
    )
    assert message == ""
    assert calls == [("remove", service.BackendType.SING_BOX), ("add", service.BackendType.SING_BOX)]


@pytest.mark.asyncio
async def test_a_failed_backend_restart_is_reported_and_not_added():
    import PasarGuardNodeBridge.common.service_pb2 as service

    added = []

    class Node:
        async def list_backends(self):
            return SimpleNamespace(types=[service.BackendType.SING_BOX])

        async def remove_backend(self, backend_type, **_):
            raise RuntimeError("stuck")

        async def add_backend(self, **kwargs):
            added.append(kwargs)

    core = SimpleNamespace(type=CoreType.singbox, to_str=lambda: "{}")
    message = await NodeOperation._add_extra_cores(
        Node(), node(core_id=1, extra=[2]), [(2, core, [])], restart_running=True
    )
    assert "core 2" in message and "stuck" in message
    assert added == []


@pytest.mark.asyncio
async def test_a_healthy_node_is_force_started_instead_of_attached():
    from PasarGuardNodeBridge.storage import LifecycleStatus

    calls = []

    class HealthyNode:
        async def get_lifecycle_state(self):
            return SimpleNamespace(observed=LifecycleStatus.HEALTHY, desired=LifecycleStatus.HEALTHY, epoch=1)

        async def info(self):
            return SimpleNamespace(started=True, node_version="0.8.1", core_version="25.1.1")

        async def connect(self, node_version, core_version):
            calls.append("connect")

        async def update_observed_lifecycle(self, observed, expected_epoch=None):
            calls.append("observe")

        async def start(self, **kwargs):
            calls.append(("start", kwargs))
            return SimpleNamespace(node_version="0.8.1", core_version="25.1.1")

    core = SimpleNamespace(type=CoreType.xray, to_str=lambda: "{}", exclude_inbound_tags=set())
    db_node = node(core_id=1)
    db_node.keep_alive = 0

    attached = await NodeOperation._start_or_attach_node(HealthyNode(), db_node, core, [], None)
    assert attached.node_version == "0.8.1"
    assert "connect" in calls and not any(isinstance(c, tuple) and c[0] == "start" for c in calls)

    calls.clear()
    started = await NodeOperation._start_or_attach_node(HealthyNode(), db_node, core, [], None, force_start=True)
    assert started.node_version == "0.8.1"
    assert [c[0] for c in calls if isinstance(c, tuple)] == ["start"]
    assert "connect" not in calls


@pytest.mark.asyncio
async def test_a_connected_node_with_failed_extras_still_raises_an_error_notification(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from app.db.models import NodeStatus
    from app.operation import node as node_op_module

    op = NodeOperation.__new__(NodeOperation)

    async def _connect_node(db_node, core, users, extra_cores=None, *, force_start: bool = False):
        return {
            "node_id": db_node.id,
            "status": NodeStatus.connected,
            "message": "core 2: boom",
            "xray_version": "",
            "node_version": "",
            "old_status": NodeStatus.connecting,
        }

    monkeypatch.setattr(node_op_module, "node_manager", MagicMock(update_node=AsyncMock()))
    monkeypatch.setattr(NodeOperation, "_get_core_users_map", AsyncMock(return_value=({1: object()}, {1: []})))
    monkeypatch.setattr(NodeOperation, "connect_node", staticmethod(_connect_node))
    monkeypatch.setattr(node_op_module, "bulk_update_node_status", AsyncMock())
    monkeypatch.setattr(node_op_module.notification, "connect_node", AsyncMock())
    monkeypatch.setattr(node_op_module.notification, "error_node", AsyncMock())

    nodes = [
        SimpleNamespace(id=1, status=NodeStatus.connecting, core_config_id=1, additional_core_config_ids=None, name="n1")
    ]
    await op._connect_nodes_bulk_local(MagicMock(), nodes)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert node_op_module.notification.connect_node.call_count == 1
    assert node_op_module.notification.error_node.call_count == 1
    assert node_op_module.notification.error_node.call_args.args[0].message == "core 2: boom"


@pytest.mark.asyncio
async def test_a_cleanly_connected_node_raises_no_error_notification(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from app.db.models import NodeStatus
    from app.operation import node as node_op_module

    op = NodeOperation.__new__(NodeOperation)

    async def _connect_node(db_node, core, users, extra_cores=None, *, force_start: bool = False):
        return {
            "node_id": db_node.id,
            "status": NodeStatus.connected,
            "message": "",
            "xray_version": "",
            "node_version": "",
            "old_status": NodeStatus.connecting,
        }

    monkeypatch.setattr(node_op_module, "node_manager", MagicMock(update_node=AsyncMock()))
    monkeypatch.setattr(NodeOperation, "_get_core_users_map", AsyncMock(return_value=({1: object()}, {1: []})))
    monkeypatch.setattr(NodeOperation, "connect_node", staticmethod(_connect_node))
    monkeypatch.setattr(node_op_module, "bulk_update_node_status", AsyncMock())
    monkeypatch.setattr(node_op_module.notification, "connect_node", AsyncMock())
    monkeypatch.setattr(node_op_module.notification, "error_node", AsyncMock())

    nodes = [
        SimpleNamespace(id=1, status=NodeStatus.connecting, core_config_id=1, additional_core_config_ids=None, name="n1")
    ]
    await op._connect_nodes_bulk_local(MagicMock(), nodes)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert node_op_module.notification.connect_node.call_count == 1
    assert node_op_module.notification.error_node.call_count == 0


@pytest.mark.asyncio
async def test_concurrent_connects_of_the_same_node_never_overlap_starts(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from app.db.models import NodeStatus
    from app.operation import node as node_op_module

    current = 0
    peak = 0

    class SlowNode:
        async def get_lifecycle_state(self):
            return None

        async def start(self, **kwargs):
            nonlocal current, peak
            current += 1
            peak = max(peak, current)
            await asyncio.sleep(0.02)
            current -= 1
            return SimpleNamespace(node_version="0.8.1", core_version="25.1.1")

        async def list_backends(self):
            return None

    monkeypatch.setattr(node_op_module, "node_manager", MagicMock(get_node=AsyncMock(return_value=SlowNode())))

    db_node = SimpleNamespace(
        id=7, name="de-1", status=NodeStatus.connecting, core_config_id=1, additional_core_config_ids=None, keep_alive=0
    )
    core = SimpleNamespace(type=CoreType.xray, to_str=lambda: "{}", exclude_inbound_tags=set())

    first, second = await asyncio.gather(
        NodeOperation.connect_node(db_node, core, []),
        NodeOperation.connect_node(db_node, core, []),
    )

    assert peak == 1, "two connects of one node ran their Start RPCs concurrently"
    assert first["status"] == NodeStatus.connected
    assert second["status"] == NodeStatus.connected
