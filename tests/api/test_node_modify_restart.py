import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import status
from PasarGuardNodeBridge import Health

from app.db.models import NodeStatus
from app.node import node_manager
from app.operation.node import NodeOperation
from app.routers import node as node_router
from tests.api import client
from tests.api.helpers import auth_headers, create_core, delete_core, unique_name
from tests.api.test_node import VALID_CERTIFICATE


class _LiveBridge:
    def __init__(self, registry: list, **kwargs):
        self.name = kwargs["name"]
        self._extra = dict(kwargs["extra"])
        self.stop_calls = 0
        self.start_calls = 0
        registry.append(self)

    async def get_extra(self):
        return self._extra

    async def set_health(self, health):
        return None

    async def get_health(self):
        return Health.HEALTHY

    async def get_versions(self):
        return "0.9.0", "25.9.0"

    async def stop(self, timeout=None):
        self.stop_calls += 1

    async def start(self, **kwargs):
        self.start_calls += 1


async def _no_bridge_memory():
    return None


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch, access_token):
    bridges: list[_LiveBridge] = []
    scheduled: list[tuple[int, bool]] = []
    operator = node_router.node_operator

    def record_connect(node_id: int, *, force_start: bool = False):
        scheduled.append((node_id, force_start))
        return asyncio.sleep(0)

    monkeypatch.setattr("app.node.ensure_bridge_memory", _no_bridge_memory)
    monkeypatch.setattr("app.node.get_bridge_memory", lambda: (None, None, None))
    monkeypatch.setattr("app.node.create_node", lambda **kwargs: _LiveBridge(bridges, **kwargs))
    monkeypatch.setattr(node_manager, "_nodes", {})
    monkeypatch.setattr(node_manager, "_node_signatures", {})
    monkeypatch.setattr(node_manager, "_user_sync_locks", {})
    monkeypatch.setattr(operator, "_update_node_impl", operator._update_node_local)
    monkeypatch.setattr(operator, "_remove_node_impl", AsyncMock())
    monkeypatch.setattr(operator, "_connect_single_node_background", record_connect)
    monkeypatch.setattr("app.operation.node.notification.create_node", AsyncMock())
    monkeypatch.setattr("app.operation.node.notification.modify_node", AsyncMock())
    monkeypatch.setattr("app.operation.node.notification.remove_node", AsyncMock())

    core = create_core(access_token)
    other_core = create_core(access_token)
    payload = {
        "name": unique_name("edge"),
        "address": "edge.example.com",
        "port": 62050,
        "api_port": 62051,
        "usage_coefficient": 1.0,
        "server_ca": VALID_CERTIFICATE,
        "connection_type": "grpc",
        "keep_alive": 60,
        "core_config_id": core["id"],
        "additional_core_config_ids": [],
        "api_key": str(uuid4()),
        "data_limit": 0,
        "data_limit_reset_strategy": "no_reset",
        "reset_time": -1,
        "default_timeout": 10,
        "internal_timeout": 15,
        "proxy_url": None,
    }
    created = client.post("/api/node", headers=auth_headers(access_token), json=payload)
    assert created.status_code == status.HTTP_201_CREATED, created.text
    node = created.json()
    try:
        yield SimpleNamespace(
            node=node,
            payload=payload,
            bridges=bridges,
            scheduled=scheduled,
            other_core_id=other_core["id"],
        )
    finally:
        client.delete(f"/api/node/{node['id']}", headers=auth_headers(access_token))
        delete_core(access_token, other_core["id"])
        delete_core(access_token, core["id"])


def _modify(access_token: str, node_id: int, body: dict):
    response = client.put(f"/api/node/{node_id}", headers=auth_headers(access_token), json=body)
    assert response.status_code == status.HTTP_200_OK, response.text
    return response.json()


def test_a_dashboard_rename_with_unchanged_start_fields_neither_restarts_nor_stops(access_token, harness):
    node_id = harness.node["id"]
    assert harness.scheduled == [(node_id, False)]
    bridge = harness.bridges[0]

    body = dict(harness.payload, name=unique_name("edge-renamed"), usage_coefficient=2.5)
    modified = _modify(access_token, node_id, body)

    assert modified["name"] == body["name"]
    assert harness.scheduled == [(node_id, False), (node_id, False)]
    assert len(harness.bridges) == 1
    assert bridge.stop_calls == 0
    assert bridge.name == body["name"]
    assert asyncio.run(bridge.get_extra())["usage_coefficient"] == 2.5

    replay = asyncio.run(
        NodeOperation.connect_node(
            SimpleNamespace(id=node_id, name=body["name"], status=NodeStatus.connecting),
            SimpleNamespace(type="xray"),
            [],
            force_start=harness.scheduled[-1][1],
        )
    )
    assert replay["status"] == NodeStatus.connected
    assert bridge.stop_calls == 0
    assert bridge.start_calls == 0


def test_a_core_change_still_forces_a_start(access_token, harness):
    node_id = harness.node["id"]

    body = dict(harness.payload, core_config_id=harness.other_core_id)
    _modify(access_token, node_id, body)

    assert harness.scheduled[-1] == (node_id, True)


def test_a_keep_alive_change_still_forces_a_start(access_token, harness):
    node_id = harness.node["id"]

    body = dict(harness.payload, keep_alive=90)
    _modify(access_token, node_id, body)

    assert harness.scheduled[-1] == (node_id, True)
