import pytest

from app.db.models import Node
from app.node import NodeManager


class _RecordingBridge:
    def __init__(self, registry: list, **kwargs):
        self.kwargs = kwargs
        self.name = kwargs["name"]
        self._extra = dict(kwargs["extra"])
        self.stop_calls = 0
        self.health_changes = []
        registry.append(self)

    async def get_extra(self):
        return self._extra

    async def set_health(self, health):
        self.health_changes.append(health)

    async def stop(self, timeout=None):
        self.stop_calls += 1


async def _no_bridge_memory():
    return None


def _node(**overrides) -> Node:
    fields = {
        "name": "edge-a",
        "address": "node-a.example",
        "port": 62050,
        "api_port": 62051,
        "server_ca": "ca",
        "api_key": "key",
        "core_config_id": None,
        "usage_coefficient": 1.0,
    }
    fields.update(overrides)
    node = Node(**fields)
    node.id = 7
    return node


@pytest.fixture
def constructed(monkeypatch: pytest.MonkeyPatch) -> list:
    registry: list = []
    monkeypatch.setattr("app.node.ensure_bridge_memory", _no_bridge_memory)
    monkeypatch.setattr("app.node.get_bridge_memory", lambda: (None, None, None))
    monkeypatch.setattr("app.node.create_node", lambda **kwargs: _RecordingBridge(registry, **kwargs))
    return registry


async def test_a_name_and_coefficient_change_updates_the_live_bridge_in_place(constructed: list):
    manager = NodeManager()

    first = await manager.update_node(_node())
    updated = await manager.update_node(_node(name="edge-a-renamed", usage_coefficient=2.5))

    assert len(constructed) == 1
    assert updated is first
    assert first.stop_calls == 0
    assert first.health_changes == []
    assert first.name == "edge-a-renamed"
    extra = await first.get_extra()
    assert extra["usage_coefficient"] == 2.5
    assert extra["id"] == 7
    assert await manager.get_node(7) is first


async def test_only_a_name_change_keeps_the_bridge(constructed: list):
    manager = NodeManager()

    first = await manager.update_node(_node())
    renamed = await manager.update_node(_node(name="edge-a-2"))

    assert renamed is first
    assert len(constructed) == 1
    assert first.stop_calls == 0
    assert first.name == "edge-a-2"
    assert (await first.get_extra())["usage_coefficient"] == 1.0


async def test_only_a_coefficient_change_keeps_the_bridge(constructed: list):
    manager = NodeManager()

    first = await manager.update_node(_node())
    recoefficient = await manager.update_node(_node(usage_coefficient=0.5))

    assert recoefficient is first
    assert len(constructed) == 1
    assert first.stop_calls == 0
    assert first.name == "edge-a"
    assert (await first.get_extra())["usage_coefficient"] == 0.5


async def test_an_empty_proxy_stored_as_blank_or_null_is_the_same_connection(constructed: list):
    manager = NodeManager()

    first = await manager.update_node(_node(proxy_url=""))
    renamed = await manager.update_node(_node(proxy_url=None, name="edge-a-null-proxy"))

    assert renamed is first
    assert len(constructed) == 1
    assert first.stop_calls == 0
    assert first.name == "edge-a-null-proxy"


async def test_a_proxy_change_still_rebuilds_the_bridge(constructed: list):
    manager = NodeManager()

    first = await manager.update_node(_node(proxy_url=None))
    rebuilt = await manager.update_node(_node(proxy_url="socks5://proxy.example:1080"))

    assert rebuilt is not first
    assert len(constructed) == 2
    assert first.stop_calls == 1


async def test_an_address_change_still_rebuilds_and_stops_the_old_bridge(constructed: list):
    manager = NodeManager()

    first = await manager.update_node(_node())
    rebuilt = await manager.update_node(_node(address="node-b.example", name="edge-b", usage_coefficient=3.0))

    assert len(constructed) == 2
    assert rebuilt is not first
    assert rebuilt is constructed[1]
    assert first.stop_calls == 1
    assert rebuilt.stop_calls == 0
    assert rebuilt.name == "edge-b"
    assert (await rebuilt.get_extra())["usage_coefficient"] == 3.0
    assert await manager.get_node(7) is rebuilt


async def test_an_unchanged_node_reuses_the_bridge_without_a_construction(constructed: list):
    manager = NodeManager()

    first = await manager.update_node(_node())
    again = await manager.update_node(_node())

    assert again is first
    assert len(constructed) == 1
    assert first.stop_calls == 0
    assert first.health_changes == []
