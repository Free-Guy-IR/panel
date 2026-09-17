import asyncio
import time
from types import SimpleNamespace

import pytest

from app.jobs import node_checker


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
    monkeypatch.setattr(node_checker, "NODE_CHECK_SEM", asyncio.Semaphore(5))
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
    monkeypatch.setattr(node_checker, "NODE_CHECK_SEM", asyncio.Semaphore(1))
    called = []

    async def check(db_node, node):
        called.append(db_node.id)

    monkeypatch.setattr(node_checker, "process_node_health_check", check)
    await node_checker._bounded_health_check(SimpleNamespace(name="detached", id=3), None)
    assert called == []
