import pytest

from app.models.node import NodeCoreUpdate, NodeGeoFilesUpdate
from app.operation import OperatorType, node as node_operation_module
from app.operation.node import NodeOperation
from config import nats_settings

UPSTREAM_MAINTENANCE_FLOOR_SECONDS = 340.0


async def _collect_maintenance_timeouts(monkeypatch) -> dict:
    seen_timeouts = {}

    async def fake_request(action, payload, timeout=None):
        seen_timeouts[action] = timeout
        return {}

    monkeypatch.setattr(node_operation_module.node_nats_client, "request", fake_request)
    operation = NodeOperation(OperatorType.API)

    await operation._update_node_api_remote(1)
    await operation._update_core_remote(1, NodeCoreUpdate(core_version="v1.2.3"))
    await operation._update_geofiles_remote(1, NodeGeoFilesUpdate())

    assert set(seen_timeouts) == {"update_node_api", "update_core", "update_geofiles"}
    return seen_timeouts


@pytest.mark.parametrize("configured", [30.0, 339.9, 340.0])
async def test_maintenance_rpcs_keep_upstream_floor_when_configured_timeout_is_lower(monkeypatch, configured):
    monkeypatch.setattr(nats_settings, "node_rpc_timeout", configured)

    seen_timeouts = await _collect_maintenance_timeouts(monkeypatch)

    assert seen_timeouts == dict.fromkeys(seen_timeouts, UPSTREAM_MAINTENANCE_FLOOR_SECONDS)


@pytest.mark.parametrize("configured", [340.5, 600.0, 3600.0])
async def test_maintenance_rpcs_never_wait_less_than_configured_node_rpc_timeout(monkeypatch, configured):
    monkeypatch.setattr(nats_settings, "node_rpc_timeout", configured)

    seen_timeouts = await _collect_maintenance_timeouts(monkeypatch)

    assert seen_timeouts == dict.fromkeys(seen_timeouts, configured)
