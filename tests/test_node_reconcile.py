from types import SimpleNamespace

import pytest
from PasarGuardNodeBridge.common import service_pb2 as service

from app.models.core import CoreType
from app.operation.node import _BACKEND_TYPE_BY_CORE, _MULTI_INSTANCE_BACKENDS, NodeOperation


def core(core_type):
    return SimpleNamespace(type=core_type, to_str=lambda: "{}")


def node(name="de-1"):
    return SimpleNamespace(id=7, name=name)


class FakeNode:
    def __init__(self, running):
        self.running = list(running)
        self.removed = []
        self.added = []

    async def list_backends(self):
        return SimpleNamespace(types=list(self.running))

    async def remove_backend(self, backend_type, **_):
        if backend_type not in self.running:
            raise RuntimeError(f"no {backend_type} backend is running on this node")
        self.running.remove(backend_type)
        self.removed.append(backend_type)

    async def add_backend(self, backend_type, **_):
        self.running.append(backend_type)
        self.added.append(backend_type)

    async def node_version(self):
        return "0.8.1"


def test_xray_is_a_known_backend_type_even_though_it_cannot_be_an_extra():
    assert CoreType.xray not in _MULTI_INSTANCE_BACKENDS
    assert _BACKEND_TYPE_BY_CORE[CoreType.xray] is service.BackendType.XRAY


@pytest.mark.asyncio
async def test_the_primary_backend_is_never_treated_as_surplus():
    pg = FakeNode([service.BackendType.XRAY])

    message = await NodeOperation._remove_surplus_backends(pg, node(), [], CoreType.xray)

    assert message == ""
    assert pg.removed == []


@pytest.mark.asyncio
async def test_a_backend_no_longer_assigned_is_removed():
    pg = FakeNode([service.BackendType.XRAY, service.BackendType.SING_BOX])

    message = await NodeOperation._remove_surplus_backends(pg, node(), [], CoreType.xray)

    assert message == ""
    assert pg.removed == [service.BackendType.SING_BOX]
    assert pg.running == [service.BackendType.XRAY]


@pytest.mark.asyncio
async def test_a_backend_still_assigned_is_kept():
    pg = FakeNode([service.BackendType.XRAY, service.BackendType.SING_BOX])

    await NodeOperation._remove_surplus_backends(pg, node(), [(2, core(CoreType.singbox), [])], CoreType.xray)

    assert pg.removed == []


@pytest.mark.asyncio
async def test_desired_map_keys_on_backend_type():
    desired = NodeOperation._desired_extra_backends(
        [(2, core(CoreType.singbox), []), (3, core(CoreType.l2tp), []), (4, None, [])]
    )
    assert set(desired) == {service.BackendType.SING_BOX, service.BackendType.L2TP}


@pytest.mark.asyncio
async def test_a_node_that_cannot_list_backends_removes_nothing():
    class Old:
        async def list_backends(self):
            raise RuntimeError("unimplemented")

    assert await NodeOperation._remove_surplus_backends(Old(), node(), [], CoreType.xray) == ""


@pytest.mark.asyncio
async def test_an_unknown_primary_core_type_blocks_every_removal():
    pg = FakeNode([service.BackendType.XRAY, service.BackendType.SING_BOX])

    message = await NodeOperation._remove_surplus_backends(pg, node(), [], None)

    assert message == ""
    assert pg.removed == [], "with no known primary, removing anything risks taking the primary down"


@pytest.mark.asyncio
async def test_removal_order_is_deterministic():
    pg = FakeNode([service.BackendType.L2TP, service.BackendType.SING_BOX, service.BackendType.OPEN_VPN])

    await NodeOperation._remove_surplus_backends(pg, node(), [], CoreType.xray)

    assert pg.removed == sorted(pg.removed, key=lambda item: int(item))


@pytest.mark.asyncio
async def test_a_backend_type_the_panel_does_not_manage_is_left_alone():
    class NewerNode(FakeNode):
        async def list_backends(self):
            return SimpleNamespace(types=[service.BackendType.XRAY, 99])

    pg = NewerNode([service.BackendType.XRAY])

    message = await NodeOperation._remove_surplus_backends(pg, node(), [], CoreType.xray)

    assert message == ""
    assert pg.removed == [], "a type from a newer node image must not be reaped as surplus"
