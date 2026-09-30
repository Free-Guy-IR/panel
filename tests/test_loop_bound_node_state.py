import asyncio
from types import SimpleNamespace

from app.node import nats_memory, node_manager

seen_loops = []
clients_made = []


async def test_one_test_loop_takes_the_node_manager_lock():
    seen_loops.append(asyncio.get_running_loop())
    assert await node_manager.get_node(-1) is None


async def test_the_next_test_loop_takes_the_node_manager_lock_as_well():
    seen_loops.append(asyncio.get_running_loop())
    assert seen_loops[0] is not seen_loops[-1]
    assert await node_manager.get_node(-1) is None


def _bridge_memory_on_this_loop(monkeypatch):
    async def create_client():
        client = SimpleNamespace(loop=asyncio.get_running_loop())
        clients_made.append(client)
        return client

    async def jetstream(client):
        return client

    async def bucket(js, name):
        return SimpleNamespace(client=js, name=name)

    monkeypatch.setattr(nats_memory, "needs_shared_bridge_memory", lambda: True)
    monkeypatch.setattr(nats_memory, "create_nats_client", create_client)
    monkeypatch.setattr(nats_memory, "get_jetstream_context", jetstream)
    monkeypatch.setattr(nats_memory, "get_or_create_kv_bucket", bucket)


async def test_one_test_loop_builds_the_shared_bridge_memory(monkeypatch):
    _bridge_memory_on_this_loop(monkeypatch)
    store, coordinator = await nats_memory.ensure_bridge_memory()
    assert store is not None and coordinator is not None
    assert clients_made[-1].loop is asyncio.get_running_loop()


async def test_the_next_test_loop_gets_bridge_memory_built_on_its_own_loop(monkeypatch):
    _bridge_memory_on_this_loop(monkeypatch)
    store, coordinator = await nats_memory.ensure_bridge_memory()
    assert store is not None and coordinator is not None
    assert len(clients_made) == 2
    assert clients_made[-1].loop is asyncio.get_running_loop()
