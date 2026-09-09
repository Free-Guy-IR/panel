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
