from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from PasarGuardNodeBridge import NodeAPIError

from app.models.node import NodeCoreUpdate, NodeGeoFilesUpdate
from app.operation import OperatorType, node as node_operation_module
from app.operation.node import NodeOperation

BRIDGE_ERRORS = [
    pytest.param(-1, 504, id="bridge-timeout"),
    pytest.param(-2, 503, id="bridge-connection-error"),
    pytest.param(-3, 503, id="bridge-bad-version"),
    pytest.param(-4, 503, id="bridge-invalid-node"),
    pytest.param(-5, 503, id="bridge-request-error"),
    pytest.param(0, 503, id="bridge-unknown"),
    pytest.param(404, 404, id="node-404"),
    pytest.param(409, 409, id="node-409"),
    pytest.param(500, 500, id="node-500"),
]

CALLS = {
    "update_node": lambda op: op._update_node_api_local(1),
    "update_core": lambda op: op._update_core_local(1, NodeCoreUpdate(core_version="v1.2.3")),
    "update_geofiles": lambda op: op._update_geofiles_local(1, NodeGeoFilesUpdate()),
}


def _failing_node(code: int):
    async def fail(*args, **kwargs):
        raise NodeAPIError(code, f"bridge said {code}")

    return SimpleNamespace(update_node=fail, update_core=fail, update_geofiles=fail)


@pytest.mark.parametrize("action", list(CALLS))
@pytest.mark.parametrize(("bridge_code", "status"), BRIDGE_ERRORS)
async def test_local_maintenance_bridge_errors_map_to_http_status(monkeypatch, action, bridge_code, status):
    node = _failing_node(bridge_code)

    async def get_node(node_id):
        return node

    monkeypatch.setattr(node_operation_module, "node_manager", SimpleNamespace(get_node=get_node))
    operation = NodeOperation(OperatorType.API)

    with pytest.raises(HTTPException) as caught:
        await CALLS[action](operation)

    assert caught.value.status_code == status
    assert caught.value.detail == f"bridge said {bridge_code}"
