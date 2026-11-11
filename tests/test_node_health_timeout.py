import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PasarGuardNodeBridge import Health

from app.db.models import NodeStatus
from app.jobs import node_checker
from app.models.settings import General, NodeHealthSettings
from app.operation.node import NodeOperation


@pytest.mark.asyncio
async def test_a_wedged_node_check_is_abandoned_instead_of_stalling_the_job(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(node_checker, "NODE_CHECK_TIMEOUT", 0.05)

    async def never_returns(db_node, node):
        await asyncio.sleep(30)

    monkeypatch.setattr(node_checker, "process_node_health_check", never_returns)

    started = time.monotonic()
    await node_checker._bounded_health_check(SimpleNamespace(name="wedged", id=1), object())
    assert time.monotonic() - started < 2


@pytest.mark.asyncio
async def test_a_healthy_node_check_still_runs_to_completion(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(node_checker, "NODE_CHECK_TIMEOUT", 5)
    seen = []

    async def quick(db_node, node):
        seen.append(db_node.id)

    monkeypatch.setattr(node_checker, "process_node_health_check", quick)

    await node_checker._bounded_health_check(SimpleNamespace(name="fine", id=7), object())
    assert seen == [7]


@pytest.mark.asyncio
async def test_one_wedged_node_does_not_starve_the_others(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(node_checker, "NODE_CHECK_TIMEOUT", 0.05)
    finished = []

    async def mixed(db_node, node):
        if db_node.name == "wedged":
            await asyncio.sleep(30)
        finished.append(db_node.id)

    monkeypatch.setattr(node_checker, "process_node_health_check", mixed)

    nodes = [SimpleNamespace(name="wedged", id=1), SimpleNamespace(name="fine", id=2)]
    started = time.monotonic()
    await asyncio.gather(*[node_checker._bounded_health_check(n, object()) for n in nodes])
    assert finished == [2]
    assert time.monotonic() - started < 2


@pytest.mark.asyncio
async def test_five_wedged_nodes_do_not_eat_the_deadline_of_the_ones_behind_them(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(node_checker, "NODE_CHECK_TIMEOUT", 0.2)
    monkeypatch.setattr(node_checker, "_CHECK_SLOTS", asyncio.Semaphore(5))
    finished = []

    async def mixed(db_node, node):
        if db_node.name.startswith("wedged"):
            await asyncio.sleep(30)
        await asyncio.sleep(0.05)
        finished.append(db_node.id)

    monkeypatch.setattr(node_checker, "process_node_health_check", mixed)

    nodes = [SimpleNamespace(name=f"wedged-{i}", id=i) for i in range(5)]
    nodes += [SimpleNamespace(name=f"fine-{i}", id=100 + i) for i in range(3)]

    started = time.monotonic()
    await asyncio.gather(*[node_checker._bounded_health_check(n, object()) for n in nodes])
    elapsed = time.monotonic() - started

    assert finished == [100, 101, 102]
    assert elapsed < 3


@pytest.mark.asyncio
async def test_a_node_that_is_not_attached_is_skipped_without_taking_a_slot(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(node_checker, "_CHECK_SLOTS", asyncio.Semaphore(1))
    called = []

    async def check(db_node, node):
        called.append(db_node.id)

    monkeypatch.setattr(node_checker, "process_node_health_check", check)
    await node_checker._bounded_health_check(SimpleNamespace(name="detached", id=3), None)
    assert called == []


class _ProbedNode:
    def __init__(self, wedged: bool):
        self.wedged = wedged
        self.probes = 0
        self.health_sets: list = []

    async def get_health(self):
        return Health.HEALTHY

    async def set_health(self, health):
        self.health_sets.append(health)

    async def get_backend_stats(self, timeout=None):
        self.probes += 1
        if self.wedged:
            await asyncio.sleep(30)

    def requires_hard_reset(self):
        return False


class _NullDB:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


def _connected(node_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=node_id, name=f"node-{node_id}", status=NodeStatus.connected, message="", default_timeout=10
    )


@pytest.mark.asyncio
async def test_real_checks_queued_behind_five_wedged_probes_keep_their_whole_deadline(monkeypatch: pytest.MonkeyPatch):
    node_health = NodeHealthSettings.model_construct(
        fail_threshold=1, recover_threshold=1, probe_timeout=5, max_backoff=60
    )
    general = General.model_construct(node_health=node_health)
    monkeypatch.setattr(node_checker, "general_settings", AsyncMock(return_value=general))
    monkeypatch.setattr(node_checker, "NODE_CHECK_TIMEOUT", 0.4)
    monkeypatch.setattr(node_checker, "NODE_CHECK_SEM", asyncio.Semaphore(5))
    monkeypatch.setattr(node_checker, "_CHECK_SLOTS", asyncio.Semaphore(5))
    for state in ("_health_streaks", "_probe_backoffs", "_core_starting_since", "_version_refresh_deadlines"):
        monkeypatch.setattr(node_checker, state, {})
    monkeypatch.setattr(node_checker, "_owns_health_checks", lambda: True)
    monkeypatch.setattr(node_checker, "needs_shared_bridge_memory", lambda: False)
    monkeypatch.setattr(node_checker, "after_healthy_node_check", AsyncMock())
    monkeypatch.setattr(node_checker, "GetDB", lambda: _NullDB())
    status_writes = []

    async def record_status(db, node_id, status, message="", **kwargs):
        status_writes.append(node_id)

    monkeypatch.setattr(NodeOperation, "_update_single_node_status", staticmethod(record_status))

    checks = [(_connected(9300 + i), _ProbedNode(wedged=True)) for i in range(5)]
    checks += [(_connected(9400 + i), _ProbedNode(wedged=False)) for i in range(3)]

    started = time.monotonic()
    await asyncio.gather(*[node_checker._bounded_health_check(db_node, node) for db_node, node in checks])
    elapsed = time.monotonic() - started

    wedged = {db_node.id for db_node, node in checks if node.wedged}
    fine = [node for _, node in checks if not node.wedged]
    assert set(status_writes) == wedged
    assert all(node.probes == 1 and Health.BROKEN not in node.health_sets for node in fine)
    assert elapsed < 3
