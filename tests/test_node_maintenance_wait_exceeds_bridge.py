import logging

import pytest
from PasarGuardNodeBridge import controller as bridge_controller

from app.models.node import NodeCoreUpdate, NodeGeoFilesUpdate
from app.operation import OperatorType, node as node_operation_module
from app.operation.node import NodeOperation
from config import nats_settings

CORE_UPDATE = NodeCoreUpdate(core_version="v1.2.3")
GEOFILES_UPDATE = NodeGeoFilesUpdate()

LOCAL_IMPL_FOR_ACTION = {
    "update_node_api": ("_update_node_api_local", ()),
    "update_core": ("_update_core_local", (CORE_UPDATE,)),
    "update_geofiles": ("_update_geofiles_local", (GEOFILES_UPDATE,)),
}


class _OkResponse:
    status_code = 200

    def json(self):
        return {}


class _RecordingBridgeNode(bridge_controller.Controller):
    def __init__(self):
        self.name = "wait-probe"
        self.node_id = 1
        self.logger = logging.getLogger("wait-probe")
        self.request_timeouts = []

    async def _make_json_request(self, method, endpoint, timeout=None, json=None):
        assert timeout is not None
        self.request_timeouts.append(timeout)
        return _OkResponse()

    async def _acquire_lifecycle_lease(self, operation):
        return object()

    async def _release_lifecycle_lease(self, lease, *args, **kwargs):
        return None


async def _panel_rpc_timeouts(monkeypatch) -> dict:
    seen = {}

    async def fake_request(action, payload, timeout=None):
        seen[action] = timeout
        return {}

    monkeypatch.setattr(node_operation_module.node_nats_client, "request", fake_request)
    operation = NodeOperation(OperatorType.API)
    await operation._update_node_api_remote(1)
    await operation._update_core_remote(1, CORE_UPDATE)
    await operation._update_geofiles_remote(1, GEOFILES_UPDATE)
    return seen


async def _bridge_worst_case_waits(monkeypatch) -> dict:
    worst = {}
    for action, (local_impl, args) in LOCAL_IMPL_FOR_ACTION.items():
        bridge_node = _RecordingBridgeNode()

        async def get_node(node_id, bridge_node=bridge_node):
            return bridge_node

        monkeypatch.setattr(node_operation_module.node_manager, "get_node", get_node)
        operation = NodeOperation(OperatorType.SYSTEM)
        await getattr(operation, local_impl)(1, *args)
        assert len(bridge_node.request_timeouts) >= 2, (action, bridge_node.request_timeouts)
        worst[action] = float(sum(bridge_node.request_timeouts))
    return worst


async def test_bridge_worst_case_counts_connectivity_check_and_maintenance_post(monkeypatch):
    worst = await _bridge_worst_case_waits(monkeypatch)
    operation_for_action = {
        "update_node_api": bridge_controller.LifecycleOperation.UPDATE_NODE,
        "update_core": bridge_controller.LifecycleOperation.UPDATE_CORE,
        "update_geofiles": bridge_controller.LifecycleOperation.UPDATE_GEOFILES,
    }
    for action, operation in operation_for_action.items():
        assert worst[action] > bridge_controller.MAINTENANCE_TIMEOUTS[operation]


@pytest.mark.parametrize("configured", [None, 10.0, 335.0, 339.9, 340.0, 341.0, 600.0])
async def test_panel_maintenance_wait_exceeds_bridge_worst_case(monkeypatch, configured):
    if configured is not None:
        monkeypatch.setattr(nats_settings, "node_rpc_timeout", configured)

    panel_waits = await _panel_rpc_timeouts(monkeypatch)
    bridge_waits = await _bridge_worst_case_waits(monkeypatch)

    assert set(panel_waits) == set(LOCAL_IMPL_FOR_ACTION)
    for action, panel_wait in panel_waits.items():
        assert panel_wait > bridge_waits[action], (action, panel_wait, bridge_waits[action])
