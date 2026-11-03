import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PasarGuardNodeBridge import Health, NodeAPIError
from PasarGuardNodeBridge.storage import LifecycleStatus

from app.operation import node as node_operation
from app.operation.node import NodeOperation


def _pg_node(health: Health, observed: LifecycleStatus | None, backend_stats_error: Exception | None):
    state = None
    if observed is not None:
        state = SimpleNamespace(observed=observed, desired=LifecycleStatus.HEALTHY, epoch=3)
    node = SimpleNamespace(
        get_lifecycle_state=AsyncMock(return_value=state),
        info=AsyncMock(return_value=SimpleNamespace(started=True, node_version="0.5.5", core_version="25.10.1")),
        get_health=AsyncMock(return_value=health),
        get_backend_stats=AsyncMock(side_effect=backend_stats_error),
        connect=AsyncMock(),
        update_observed_lifecycle=AsyncMock(),
        _work_available=asyncio.Event(),
        _ensure_sync_worker_running=AsyncMock(),
    )
    return node


def _assert_shared_sync_woken(node, shared: bool):
    assert node._work_available.is_set() is shared
    if shared:
        node._ensure_sync_worker_running.assert_awaited_once()
    else:
        node._ensure_sync_worker_running.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("health", "observed"),
    [
        (Health.BROKEN, None),
        (Health.HEALTHY, LifecycleStatus.BROKEN),
    ],
)
async def test_broken_backend_that_still_fails_is_not_reattached(health, observed):
    node = _pg_node(health, observed, NodeAPIError(500, "wg0: no such device"))

    result = await NodeOperation._attach_if_running(node, "n1")

    assert result is None
    node.get_backend_stats.assert_awaited_once()
    node.connect.assert_not_awaited()
    node.update_observed_lifecycle.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("shared", [False, True])
async def test_recovered_broken_backend_is_reattached(monkeypatch, shared):
    monkeypatch.setattr(node_operation, "needs_shared_bridge_memory", lambda: shared)
    node = _pg_node(Health.BROKEN, None, None)

    result = await NodeOperation._attach_if_running(node, "n1")

    assert result is not None
    node.get_backend_stats.assert_awaited_once()
    node.connect.assert_awaited_once_with("0.5.5", "25.10.1")
    _assert_shared_sync_woken(node, shared)


@pytest.mark.asyncio
@pytest.mark.parametrize("shared", [False, True])
async def test_healthy_backend_is_reattached_without_a_stats_probe(monkeypatch, shared):
    monkeypatch.setattr(node_operation, "needs_shared_bridge_memory", lambda: shared)
    node = _pg_node(Health.HEALTHY, LifecycleStatus.HEALTHY, None)

    result = await NodeOperation._attach_if_running(node, "n1")

    assert result is not None
    node.get_backend_stats.assert_not_awaited()
    node.connect.assert_awaited_once()
    node.update_observed_lifecycle.assert_awaited_once_with(LifecycleStatus.HEALTHY, expected_epoch=3)
    _assert_shared_sync_woken(node, shared)


@pytest.mark.asyncio
@pytest.mark.parametrize("probe_fails", [True, False])
async def test_concurrent_reconnects_of_a_broken_backend_agree_with_the_probe(monkeypatch, probe_fails):
    from app.models.core import CoreType

    monkeypatch.setattr(node_operation, "needs_shared_bridge_memory", lambda: False)
    calls = []
    probe_gate = asyncio.Event()
    probes_in_flight = []

    class BrokenBackendNode:
        async def get_lifecycle_state(self):
            return SimpleNamespace(observed=LifecycleStatus.HEALTHY, desired=LifecycleStatus.HEALTHY, epoch=4)

        async def get_health(self):
            return Health.BROKEN

        async def info(self):
            return SimpleNamespace(started=True, node_version="0.5.5", core_version="25.10.1")

        async def get_backend_stats(self):
            probes_in_flight.append(1)
            await probe_gate.wait()
            if probe_fails:
                raise NodeAPIError(500, "wg0: no such device")

        async def connect(self, node_version, core_version):
            calls.append("connect")

        async def update_observed_lifecycle(self, observed, expected_epoch=None):
            calls.append("observe")

        async def sync_users(self, users, flush_pending=False):
            calls.append("sync_users")

        async def start(self, **kwargs):
            calls.append("start")
            return SimpleNamespace(node_version="0.5.5", core_version="25.10.1")

    pg_node = BrokenBackendNode()
    db_node = SimpleNamespace(id=9, name="n1", keep_alive=0, core_config_id=1, additional_core_config_ids=None)
    core = SimpleNamespace(type=CoreType.xray, to_str=lambda: "{}", exclude_inbound_tags=set())

    first = asyncio.create_task(NodeOperation._start_or_attach_node(pg_node, db_node, core, [], None))
    second = asyncio.create_task(NodeOperation._start_or_attach_node(pg_node, db_node, core, [], None))
    for _ in range(20):
        if len(probes_in_flight) == 2:
            break
        await asyncio.sleep(0)

    assert len(probes_in_flight) == 2
    assert "connect" not in calls and "start" not in calls

    probe_gate.set()
    results = await asyncio.gather(first, second)

    assert [r.node_version for r in results] == ["0.5.5", "0.5.5"]
    if probe_fails:
        assert calls.count("start") == 2
        assert "connect" not in calls and "observe" not in calls
    else:
        assert calls.count("connect") == 2
        assert "start" not in calls
