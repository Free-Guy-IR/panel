import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PasarGuardNodeBridge import Health, NodeAPIError

from app.db.models import NodeStatus
from app.jobs import node_checker
from app.models.settings import NodeHealthSettings
from app.operation import node as node_operation
from app.operation.node import NodeOperation


class _NullDB:
    bind = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _OldBridge:
    def __init__(self, error, *, hang_in="stats"):
        self.error = error
        self.hang_in = hang_in
        self.gate = asyncio.Event()
        self.probe_started = asyncio.Event()
        self.health_sets: list = []

    async def _hang(self):
        self.probe_started.set()
        await self.gate.wait()
        raise self.error

    async def get_health(self):
        if self.hang_in == "health":
            await self._hang()
        return Health.HEALTHY

    async def get_backend_stats(self, timeout=None):
        await self._hang()

    async def set_health(self, health):
        self.health_sets.append(health)

    async def get_lifecycle_state(self):
        return None

    async def update_observed_lifecycle(self, *args, **kwargs):
        return None


@pytest.fixture
def wired(monkeypatch):
    node_health = NodeHealthSettings.model_construct(
        fail_threshold=1, recover_threshold=1, probe_timeout=5, max_backoff=60
    )
    monkeypatch.setattr(node_checker, "_node_health_settings", AsyncMock(return_value=node_health))
    monkeypatch.setattr(node_checker, "_health_streaks", {})
    monkeypatch.setattr(node_checker, "_probe_backoffs", {})
    writes: list = []

    async def record(db, node_id, status, message="", **kwargs):
        writes.append((status, message))

    async def no_hold(db_node, health):
        return False

    async def reconnect(db, node_id, **kwargs):
        writes.append(("reconnect-requested", node_id))

    monkeypatch.setattr(NodeOperation, "_update_single_node_status", staticmethod(record))
    monkeypatch.setattr(node_checker, "GetDB", lambda: _NullDB())
    monkeypatch.setattr(node_operation, "GetDB", lambda: _NullDB())
    monkeypatch.setattr(node_checker, "hold_for_usage_collection", no_hold)
    monkeypatch.setattr(node_checker.node_operator, "connect_single_node", reconnect)
    return writes


async def _restart(bridge, db_node, writes, monkeypatch):
    async def restarted(db_node, core, users, extra_cores, force_start, *_rest):
        await bridge.set_health(Health.HEALTHY)
        return {
            "node_id": db_node.id,
            "status": NodeStatus.connected,
            "message": "",
            "xray_version": "1",
            "node_version": "1",
            "old_status": NodeStatus.connected,
        }

    monkeypatch.setattr(NodeOperation, "_connect_node_inner", staticmethod(restarted))
    result = await NodeOperation.connect_node(db_node, None, [], [], force_start=True)
    writes.append((result["status"], result["message"]))


ERRORS = [
    pytest.param(ConnectionError("old bridge connection reset by the restart"), "stats", id="broken-transport"),
    pytest.param(NodeAPIError(503, "upstream connect error or disconnect/reset before headers"), "stats", id="5xx"),
    pytest.param(NodeAPIError(-1, "timed out"), "stats", id="api-timeout"),
    pytest.param(TimeoutError(), "health", id="get-health-timeout"),
    pytest.param(NodeAPIError(502, "bad gateway"), "health", id="get-health-api-error"),
]


@pytest.mark.parametrize(("error", "hang_in"), ERRORS)
async def test_a_probe_that_started_before_a_restart_does_not_undo_it(wired, monkeypatch, error, hang_in):
    writes = wired
    bridge = _OldBridge(error, hang_in=hang_in)
    db_node = SimpleNamespace(id=9201, name="n9201", status=NodeStatus.connected, default_timeout=5, internal_timeout=5)

    check = asyncio.create_task(node_checker.process_node_health_check(db_node, bridge))
    await bridge.probe_started.wait()
    await _restart(bridge, db_node, writes, monkeypatch)
    assert writes[-1][0] == NodeStatus.connected

    bridge.gate.set()
    await check

    statuses = [w for w in writes if w[0] in (NodeStatus.connected, NodeStatus.error)]
    assert statuses[-1][0] == NodeStatus.connected, f"stale probe overwrote the restart: {writes}"
    assert bridge.health_sets[-1] is Health.HEALTHY, f"stale probe marked the restarted bridge: {bridge.health_sets}"
    assert not any(w[0] == "reconnect-requested" for w in writes), f"stale probe asked for a second Start: {writes}"


async def test_an_abandoned_check_that_outlived_a_restart_does_not_undo_it(wired, monkeypatch):
    writes = wired
    bridge = _OldBridge(ConnectionError("never"), hang_in="stats")
    db_node = SimpleNamespace(id=9202, name="n9202", status=NodeStatus.connected, default_timeout=5, internal_timeout=5)
    monkeypatch.setattr(node_checker, "NODE_CHECK_TIMEOUT", 0.5)

    check = asyncio.create_task(node_checker._bounded_health_check(db_node, bridge))
    await bridge.probe_started.wait()
    await _restart(bridge, db_node, writes, monkeypatch)
    await check

    statuses = [w for w in writes if w[0] in (NodeStatus.connected, NodeStatus.error)]
    assert statuses[-1][0] == NodeStatus.connected, f"abandoned check overwrote the restart: {writes}"
    assert bridge.health_sets[-1] is Health.HEALTHY, (
        f"abandoned check marked the restarted bridge: {bridge.health_sets}"
    )


@pytest.mark.parametrize(("error", "hang_in"), ERRORS)
async def test_a_probe_with_no_connect_in_between_still_records_its_error(wired, error, hang_in):
    writes = wired
    bridge = _OldBridge(error, hang_in=hang_in)
    bridge.gate.set()
    db_node = SimpleNamespace(id=9203, name="n9203", status=NodeStatus.connected, default_timeout=5, internal_timeout=5)

    await node_checker.process_node_health_check(db_node, bridge)

    assert any(w[0] == NodeStatus.error for w in writes), f"a current probe error was dropped: {writes}"


async def test_an_abandoned_check_with_no_connect_in_between_is_still_recorded(wired, monkeypatch):
    writes = wired
    bridge = _OldBridge(ConnectionError("never"), hang_in="stats")
    db_node = SimpleNamespace(id=9204, name="n9204", status=NodeStatus.connected, default_timeout=5, internal_timeout=5)
    monkeypatch.setattr(node_checker, "NODE_CHECK_TIMEOUT", 0.2)

    await node_checker._bounded_health_check(db_node, bridge)

    assert any(w[0] == NodeStatus.error for w in writes), f"a current abandoned check was dropped: {writes}"
    assert bridge.health_sets[-1] is Health.BROKEN


@pytest.mark.parametrize(("error", "hang_in"), ERRORS)
async def test_a_probe_that_ends_while_a_connect_is_running_leaves_the_status_to_that_connect(wired, error, hang_in):
    writes = wired
    bridge = _OldBridge(error, hang_in=hang_in)
    db_node = SimpleNamespace(id=9205, name="n9205", status=NodeStatus.connected, default_timeout=5, internal_timeout=5)

    check = asyncio.create_task(node_checker.process_node_health_check(db_node, bridge))
    await bridge.probe_started.wait()
    async with node_operation._connect_lock(db_node.id):
        bridge.gate.set()
        await asyncio.wait_for(check, timeout=5)

    assert not writes, f"a probe wrote over a connect that was still running: {writes}"
    assert Health.BROKEN not in bridge.health_sets, f"a probe marked a bridge mid-connect: {bridge.health_sets}"


class _NotConnectedBridge(_OldBridge):
    async def get_health(self):
        return Health.NOT_CONNECTED


async def test_an_abandoned_check_whose_own_connect_hangs_is_still_recorded(wired, monkeypatch):
    from app.fork.operation.node_connect_generation import bump_connect_generation

    writes = wired
    bridge = _NotConnectedBridge(ConnectionError("never"))
    db_node = SimpleNamespace(id=9206, name="n9206", status=NodeStatus.connected, default_timeout=5, internal_timeout=5)
    monkeypatch.setattr(node_checker, "NODE_CHECK_TIMEOUT", 0.3)

    async def own_connect_hangs(db, node_id, **kwargs):
        bump_connect_generation(node_id)
        try:
            await asyncio.sleep(3600)
        finally:
            bump_connect_generation(node_id)

    monkeypatch.setattr(node_checker.node_operator, "connect_single_node", own_connect_hangs)

    await node_checker._bounded_health_check(db_node, bridge)

    assert any(w[0] == NodeStatus.error for w in writes), f"an abandoned check of its own hung connect was dropped: {writes}"
    assert bridge.health_sets[-1] is Health.BROKEN
