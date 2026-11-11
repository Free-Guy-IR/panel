import asyncio

import pytest

import app.node as node_module
from tests.test_node_remove_user_sync_lock import STUCK_FOR, TIMEOUT, _db_node, _FakeNode, _FencedFakeNode, _manager


@pytest.mark.parametrize("node_cls", [_FakeNode, _FencedFakeNode], ids=["direct", "full_sync_fence"])
@pytest.mark.asyncio
async def test_sync_started_while_removal_waits_never_reaches_the_removed_node(monkeypatch, node_cls):
    manager, probe, _created = _manager(monkeypatch, node_cls)
    manager._removal_sync_wait_seconds = 0.3
    old = await manager.update_node(_db_node())
    first = asyncio.create_task(manager.sync_full(1, [object()]))
    await asyncio.wait_for(old.entered.wait(), TIMEOUT)
    old.entered.clear()

    removal = asyncio.create_task(manager.remove_node(1, remote_stop=False))
    await asyncio.sleep(0)
    late = asyncio.create_task(manager.sync_full(1, [object()]))
    await asyncio.wait_for(removal, TIMEOUT)

    late_outlived_removal = not late.done()
    late_pushed_to_removed = old.entered.is_set()
    old.gate.set()
    first_result, late_result = await asyncio.wait_for(asyncio.gather(first, late, return_exceptions=True), TIMEOUT)
    assert not late_pushed_to_removed
    assert not late_outlived_removal
    assert isinstance(first_result, node_module.UserSyncPreemptedError)
    assert isinstance(late_result, node_module.UserSyncPreemptedError)
    assert await manager.get_node(1) is None

    new = await asyncio.wait_for(manager.update_node(_db_node()), TIMEOUT)
    assert new is not old
    second = asyncio.create_task(manager.sync_full(1, [object()]))
    third = asyncio.create_task(manager.sync_full(1, [object()]))
    await asyncio.wait_for(new.entered.wait(), TIMEOUT)
    await asyncio.sleep(0.05)
    assert probe.active == 1
    new.gate.set()
    results = await asyncio.wait_for(asyncio.gather(second, third, return_exceptions=True), TIMEOUT)
    assert results == [new, new]
    assert new.completed == 2
    assert old.completed == 0
    assert probe.peak == 1


@pytest.mark.asyncio
async def test_sync_started_while_a_stuck_removal_hits_its_deadline_is_refused_and_the_mark_clears(monkeypatch):
    manager, probe, _created = _manager(monkeypatch, _FakeNode, ignore_cancel=True)
    manager._removal_sync_wait_seconds = 0.2
    old = await manager.update_node(_db_node())
    first = asyncio.create_task(manager.sync_full(1, [object()]))
    await asyncio.wait_for(old.entered.wait(), TIMEOUT)
    old.entered.clear()
    asyncio.get_running_loop().call_later(STUCK_FOR, old.gate.set)

    removal = asyncio.create_task(manager.remove_node(1, remote_stop=False))
    await asyncio.sleep(0)
    late = asyncio.create_task(manager.sync_full(1, [object()]))
    await asyncio.wait_for(removal, TIMEOUT)

    late_outlived_removal = not late.done()
    old.gate.set()
    _first_result, late_result = await asyncio.wait_for(asyncio.gather(first, late, return_exceptions=True), TIMEOUT)
    assert not late_outlived_removal
    assert isinstance(late_result, node_module.UserSyncPreemptedError)
    assert old.completed == 1

    new = await asyncio.wait_for(manager.update_node(_db_node()), TIMEOUT)
    new.gate.set()
    assert await asyncio.wait_for(manager.sync_full(1, [object()]), TIMEOUT) is new
    assert probe.peak == 1
