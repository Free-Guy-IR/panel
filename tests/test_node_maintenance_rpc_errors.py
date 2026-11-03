from types import SimpleNamespace

import nats.errors
import pytest
from fastapi import HTTPException

from app.models.node import BulkNodeSelection, NodeCoreUpdate, NodeGeoFilesUpdate
from app.operation import OperatorType, node as node_operation_module
from app.operation.node import NodeOperation


def _worker_not_found():
    exc = RuntimeError("Node not found")
    exc.code = 404
    return exc


ERRORS = [
    pytest.param(_worker_not_found, 404, id="worker-404"),
    pytest.param(lambda: nats.errors.TimeoutError(), 504, id="nats-timeout"),
    pytest.param(lambda: nats.errors.NoRespondersError(), 503, id="no-responders"),
    pytest.param(lambda: nats.errors.ConnectionClosedError(), 503, id="connection-closed"),
    pytest.param(lambda: RuntimeError("NATS is not available"), 503, id="nats-unavailable"),
]

CALLS = {
    "update_node_api": lambda op: op._update_node_api_remote(1),
    "update_core": lambda op: op._update_core_remote(1, NodeCoreUpdate(core_version="v1.2.3")),
    "update_geofiles": lambda op: op._update_geofiles_remote(1, NodeGeoFilesUpdate()),
}


@pytest.mark.parametrize("action", list(CALLS))
@pytest.mark.parametrize(("make_error", "status"), ERRORS)
async def test_remote_maintenance_errors_map_to_http_status(monkeypatch, action, make_error, status):
    async def failing_request(rpc_action, payload, timeout=None):
        raise make_error()

    monkeypatch.setattr(node_operation_module.node_nats_client, "request", failing_request)
    operation = NodeOperation(OperatorType.API)

    with pytest.raises(HTTPException) as caught:
        await CALLS[action](operation)

    assert caught.value.status_code == status


def _bulk_operation(monkeypatch, fail_ids):
    async def request(rpc_action, payload, timeout=None):
        if payload["node_id"] in fail_ids:
            raise nats.errors.TimeoutError()
        return {}

    monkeypatch.setattr(node_operation_module.node_nats_client, "request", request)
    operation = NodeOperation(OperatorType.API)
    operation._update_node_api_impl = operation._update_node_api_remote

    async def validated_nodes(db, ids):
        return [SimpleNamespace(id=node_id, name=f"n{node_id}") for node_id in sorted(ids)]

    async def validate_and_release(db, node_id):
        return None

    operation._get_validated_nodes = validated_nodes
    operation._validate_node_then_release_db = validate_and_release
    return operation


async def test_bulk_update_continues_after_one_node_times_out(monkeypatch):
    operation = _bulk_operation(monkeypatch, fail_ids={1})

    response = await operation.bulk_update_nodes(
        None, BulkNodeSelection(ids={1, 2, 3}), SimpleNamespace(username="root")
    )

    assert response.nodes == ["n2", "n3"]
    assert response.count == 2


async def test_bulk_update_raises_first_error_when_every_node_fails(monkeypatch):
    operation = _bulk_operation(monkeypatch, fail_ids={1, 2})

    with pytest.raises(HTTPException) as caught:
        await operation.bulk_update_nodes(None, BulkNodeSelection(ids={1, 2}), SimpleNamespace(username="root"))

    assert caught.value.status_code == 504
