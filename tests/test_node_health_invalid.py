import asyncio
import datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from PasarGuardNodeBridge import Health, NodeAPIError

from app.db.models import Node, NodeConnectionType, NodeStatus
from app.jobs import node_checker
from app.node import NodeManager

NODE_ID = 41


def _ca_pem() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "node.test")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def _db_node(ca: str, api_key: str, **overrides) -> Node:
    fields = {
        "name": "tr-invalid",
        "address": "127.0.0.1",
        "port": 1,
        "api_port": 1,
        "server_ca": ca,
        "api_key": api_key,
        "core_config_id": None,
        "connection_type": NodeConnectionType.rest,
        "default_timeout": 1,
        "internal_timeout": 1,
    }
    fields.update(overrides)
    node = Node(**fields)
    node.id = NODE_ID
    return node


async def _no_bridge_memory():
    return None


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch) -> NodeManager:
    monkeypatch.setattr("app.node.ensure_bridge_memory", _no_bridge_memory)
    monkeypatch.setattr("app.node.get_bridge_memory", lambda: (None, None, None))
    return NodeManager()


@pytest.mark.asyncio
async def test_invalid_lands_only_on_the_object_a_reconfiguration_replaced(manager: NodeManager):
    ca, api_key = _ca_pem(), str(uuid4())

    retired = await manager.update_node(_db_node(ca, api_key))
    assert await retired.get_health() is Health.NOT_CONNECTED

    live = await manager.update_node(_db_node(ca, api_key, port=2))

    assert live is not retired
    assert await manager.get_node(NODE_ID) is live
    assert await retired.get_health() is Health.INVALID
    assert await live.get_health() is Health.NOT_CONNECTED


@pytest.mark.asyncio
async def test_invalid_lands_only_on_the_object_a_removal_dropped(manager: NodeManager):
    removed = await manager.update_node(_db_node(_ca_pem(), str(uuid4())))

    await manager.remove_node(NODE_ID)
    for _ in range(50):
        if await removed.get_health() is Health.INVALID:
            break
        await asyncio.sleep(0.01)

    assert await removed.get_health() is Health.INVALID
    assert await manager.get_node(NODE_ID) is None
    assert NODE_ID not in await manager.get_nodes()


@pytest.mark.asyncio
async def test_a_retired_object_can_never_serve_again(manager: NodeManager):
    ca, api_key = _ca_pem(), str(uuid4())
    retired = await manager.update_node(_db_node(ca, api_key))
    await manager.update_node(_db_node(ca, api_key, port=2))

    await retired.set_health(Health.HEALTHY)
    assert await retired.get_health() is Health.INVALID

    with pytest.raises(NodeAPIError) as refused:
        await retired.start(config="{}", backend_type=0, users=[])
    assert refused.value.code == -4


@pytest.mark.asyncio
async def test_the_checker_leaves_a_retired_object_to_the_operation_that_retired_it(monkeypatch: pytest.MonkeyPatch):
    status_writes = AsyncMock()
    reconnects = AsyncMock()
    maintenance = AsyncMock()
    monkeypatch.setattr(node_checker.NodeOperation, "_update_single_node_status", status_writes)
    monkeypatch.setattr(node_checker.node_operator, "connect_single_node", reconnects)
    monkeypatch.setattr(node_checker, "after_healthy_node_check", maintenance)

    class RetiredNode:
        def requires_hard_reset(self):
            return False

        async def get_health(self):
            return Health.INVALID

        async def get_backend_stats(self, timeout=None):
            raise AssertionError("a retired object must not be probed")

    db_node = type("DbNode", (), {"id": NODE_ID, "name": "tr-invalid", "status": NodeStatus.connected})()
    await node_checker.process_node_health_check(db_node, RetiredNode())

    status_writes.assert_not_awaited()
    reconnects.assert_not_awaited()
    maintenance.assert_not_awaited()
