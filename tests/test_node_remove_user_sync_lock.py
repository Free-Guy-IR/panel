import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.node as node_module
from app.db.models import Node

TIMEOUT = 1
STUCK_FOR = 3


class _Held:
    def check(self):
        return None


class _Probe:
    def __init__(self):
        self.active = 0
        self.peak = 0


class _FakeNode:
    def __init__(self, probe: _Probe, *, ignore_cancel: bool = False, **kwargs):
        self.kwargs = kwargs
        self.name = kwargs.get("name")
        self.probe = probe
        self.ignore_cancel = ignore_cancel
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()
        self.cancelled = False
        self.completed = 0
        self.released: list = []

    async def get_extra(self):
        return self.kwargs.get("extra", {})

    async def set_health(self, health):
        return None

    async def stop(self, timeout=None):
        return None

    async def sync_users(self, users, flush_pending=False):
        self.probe.active += 1
        self.probe.peak = max(self.probe.peak, self.probe.active)
        self.entered.set()
        try:
            while not self.gate.is_set():
                try:
                    await self.gate.wait()
                except asyncio.CancelledError:
                    self.cancelled = True
                    if not self.ignore_cancel:
                        raise
            self.completed += 1
        finally:
            self.probe.active -= 1


class _FencedFakeNode(_FakeNode):
    @asynccontextmanager
    async def full_sync_fence(self):
        yield _Held()

    async def capture_queued_work(self):
        return {"entry": 1}

    async def release_queued_work(self, captured):
        self.released.append(captured)

    async def retire_queued_work(self, captured):
        return None


def _db_node() -> Node:
    node = Node(
        name="n1",
        address="10.0.0.1",
        port=1000,
        api_port=1001,
        server_ca="ca",
        api_key="key",
        core_config_id=None,
        usage_coefficient=1.0,
    )
    node.id = 1
    return node


def _manager(monkeypatch, node_cls, **fake_kwargs):
    probe = _Probe()
    created: list[_FakeNode] = []

    def _create(**kwargs):
        fake = node_cls(probe, **fake_kwargs, **kwargs)
        created.append(fake)
        return fake

    monkeypatch.setattr(node_module, "ensure_bridge_memory", AsyncMock(return_value=None))
    monkeypatch.setattr(node_module, "get_bridge_memory", lambda: (None, None, None))
    monkeypatch.setattr(node_module, "create_node", _create)
    manager = node_module.NodeManager()
    manager.logger = MagicMock()
    return manager, probe, created


@pytest.mark.parametrize("node_cls", [_FakeNode, _FencedFakeNode], ids=["direct", "full_sync_fence"])
@pytest.mark.asyncio
async def test_remove_then_readd_never_runs_two_full_syncs_on_one_node(monkeypatch, node_cls):
    manager, probe, _created = _manager(monkeypatch, node_cls)
    old = await manager.update_node(_db_node())
    orphan = asyncio.create_task(manager.sync_full(1, [object()]))
    await asyncio.wait_for(old.entered.wait(), TIMEOUT)

    await asyncio.wait_for(manager.remove_node(1, remote_stop=False), TIMEOUT)
    new =await asyncio.wait_for(manager.update_node(_db_node()), TIMEOUT)
    assert new is not old
    second = asyncio.create_task(manager.sync_full(1, [object()]))
    try:
        await asyncio.wait_for(new.entered.wait(), 0.2)
    except TimeoutError:
        pass

    assert probe.peak == 1
    old.gate.set()
    new.gate.set()
    results = await asyncio.wait_for(asyncio.gather(orphan, second, return_exceptions=True), TIMEOUT)
    assert results[0] is not old
    assert results[1] is new
    assert probe.peak == 1


@pytest.mark.parametrize("node_cls", [_FakeNode, _FencedFakeNode], ids=["direct", "full_sync_fence"])
@pytest.mark.asyncio
async def test_remove_during_stuck_full_sync_returns_promptly_and_cancels_it(monkeypatch, node_cls):
    manager, _probe, _created = _manager(monkeypatch, node_cls)
    old = await manager.update_node(_db_node())
    orphan = asyncio.create_task(manager.sync_full(1, [object()]))
    await asyncio.wait_for(old.entered.wait(), TIMEOUT)

    await asyncio.wait_for(manager.remove_node(1, remote_stop=False), 0.5)

    assert old.cancelled is True
    old.gate.set()
    with pytest.raises(node_module.UserSyncPreemptedError):
        await asyncio.wait_for(orphan, TIMEOUT)
    assert old.completed == 0
    if node_cls is _FencedFakeNode:
        assert old.released == [{}]


@pytest.mark.asyncio
async def test_preempted_full_sync_caller_gets_user_sync_preempted_error(monkeypatch):
    manager, _probe, _created = _manager(monkeypatch, _FakeNode)
    old = await manager.update_node(_db_node())
    caller = asyncio.create_task(manager.sync_full(1, [object()]))
    await asyncio.wait_for(old.entered.wait(), TIMEOUT)

    await asyncio.wait_for(manager.remove_node(1, remote_stop=False), TIMEOUT)

    error = await asyncio.wait_for(asyncio.gather(caller, return_exceptions=True), TIMEOUT)
    assert isinstance(error[0], node_module.UserSyncPreemptedError)
    assert not caller.cancelled()
    assert "UserSyncPreemptedError" in node_module.__all__


@pytest.mark.asyncio
async def test_cancelling_the_full_sync_caller_cancels_the_sync(monkeypatch):
    manager, _probe, _created = _manager(monkeypatch, _FakeNode)
    old = await manager.update_node(_db_node())
    caller = asyncio.create_task(manager.sync_full(1, [object()]))
    await asyncio.wait_for(old.entered.wait(), TIMEOUT)

    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(caller, TIMEOUT)

    await asyncio.sleep(0)
    assert old.cancelled is True
    assert old.completed == 0
    assert manager._user_sync_tasks.get(1, set()) == set()


@pytest.mark.asyncio
async def test_remove_keeps_the_per_node_user_sync_lock_identity(monkeypatch):
    manager, _probe, _created = _manager(monkeypatch, _FakeNode)
    await manager.update_node(_db_node())
    before = manager._user_sync_locks.setdefault(1, asyncio.Lock())

    await asyncio.wait_for(manager.remove_node(1, remote_stop=False), TIMEOUT)

    assert manager._user_sync_locks.get(1) is before
    await manager.update_node(_db_node())
    assert manager._user_sync_locks.get(1) is before


@pytest.mark.asyncio
async def test_remove_waits_for_the_cancelled_sync_to_release_the_lock(monkeypatch):
    manager, _probe, _created = _manager(monkeypatch, _FakeNode)
    old = await manager.update_node(_db_node())
    orphan = asyncio.create_task(manager.sync_full(1, [object()]))
    await asyncio.wait_for(old.entered.wait(), TIMEOUT)
    lock = manager._user_sync_locks[1]

    await asyncio.wait_for(manager.remove_node(1, remote_stop=False), TIMEOUT)

    assert orphan.done()
    assert not lock.locked()
    assert manager._user_sync_tasks.get(1) is None
    assert isinstance(orphan.exception(), node_module.UserSyncPreemptedError)


@pytest.mark.asyncio
async def test_sync_ignoring_cancellation_bounds_remove_and_still_blocks_a_readded_sync(monkeypatch):
    manager, probe, created = _manager(monkeypatch, _FakeNode, ignore_cancel=True)
    monkeypatch.setattr(manager, "_removal_sync_wait_seconds", 0.2, raising=False)
    old = await manager.update_node(_db_node())
    orphan = asyncio.create_task(manager.sync_full(1, [object()]))
    second = None
    try:
        await asyncio.wait_for(old.entered.wait(), TIMEOUT)
        asyncio.get_running_loop().call_later(STUCK_FOR, old.gate.set)

        await asyncio.wait_for(manager.remove_node(1, remote_stop=False), TIMEOUT)

        assert old.cancelled is True
        assert await manager.get_node(1) is None
        readd = asyncio.create_task(manager.update_node(_db_node()))
        await asyncio.sleep(0.1)
        assert probe.active == 1
        old.gate.set()
        new = await asyncio.wait_for(readd, TIMEOUT)
        second = asyncio.create_task(manager.sync_full(1, [object()]))
        await asyncio.wait_for(new.entered.wait(), TIMEOUT)
        assert probe.peak == 1
    finally:
        for fake in created:
            fake.gate.set()
        pending = [task for task in (orphan, second) if task is not None]
        await asyncio.gather(*pending, return_exceptions=True)
    assert probe.peak == 1
