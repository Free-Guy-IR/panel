from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from PasarGuardNodeBridge import NodeAPIError

from app.db.models import NodeStatus
from app.operation import OperatorType, node as node_module
from app.operation.node import NodeOperation


@pytest.mark.parametrize("bridge_code", [401, 403, 409, 500])
async def test_sync_users_local_answers_a_node_401_with_502(monkeypatch, bridge_code):
    db_node = SimpleNamespace(id=1, status=NodeStatus.connected, core_config_id=1)
    statuses = []

    async def get_validated_node(self, db, node_id, **kwargs):
        return db_node

    async def get_node(node_id):
        return SimpleNamespace()

    async def core_users_map(self, db, ids, lazy=True):
        return {}, {}

    async def sync_full(node_id, users, flush_pending=False):
        raise NodeAPIError(bridge_code, "invalid api key")

    async def update_node_status(**kwargs):
        statuses.append(kwargs["status"])

    async def rollback():
        return None

    monkeypatch.setattr(NodeOperation, "get_validated_node", get_validated_node)
    monkeypatch.setattr(NodeOperation, "_get_core_users_map", core_users_map)
    monkeypatch.setattr(NodeOperation, "_node_core_ids", lambda self, n: [1])
    monkeypatch.setattr(NodeOperation, "_extra_cores_for", lambda self, n, a, b: [])
    monkeypatch.setattr(node_module, "node_users_loader", lambda *a, **k: [])
    monkeypatch.setattr(node_module, "update_node_status", update_node_status)
    monkeypatch.setattr(
        node_module, "node_manager", SimpleNamespace(get_node=get_node, sync_full=sync_full)
    )

    with pytest.raises(HTTPException) as caught:
        await NodeOperation(OperatorType.API)._sync_node_users_local(
            SimpleNamespace(rollback=rollback), node_id=1, flush_users=False
        )

    assert caught.value.status_code == {401: 502}.get(bridge_code, bridge_code)
