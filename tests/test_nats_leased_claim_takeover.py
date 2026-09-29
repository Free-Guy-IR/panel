import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import certifi
from PasarGuardNodeBridge.common.service_pb2 import User
from PasarGuardNodeBridge.controller import Controller

from app.node import nats_memory
from app.node.nats_memory import NatsUserSyncStore
from app.operation import node as node_operation
from tests.test_nats_sync_integration import jetstream, wait_for_keys  # noqa: F401


async def test_a_worker_keeps_polling_a_live_foreign_lease_and_takes_it_over_once_it_expires(jetstream, monkeypatch):  # noqa: F811
    store = NatsUserSyncStore(jetstream.kv)
    now = [time.time()]
    monkeypatch.setattr(nats_memory, "time", SimpleNamespace(time=lambda: now[0]))
    await store.enqueue_users("93", [User(email="orphan")])
    assert len(await store.claim_users("93", "departed-worker", 1, 60)) == 1
    node = Controller(
        server_ca=certifi.contents(),
        api_key=str(uuid4()),
        service_url="https://localhost:1",
        node_id="93",
        user_sync_store=store,
        sync_poll_interval=0.01,
    )
    node._worker_idle_timeout = 0.02
    monkeypatch.setattr(node_operation, "needs_shared_bridge_memory", lambda: True)
    sync = AsyncMock(return_value=[])
    monkeypatch.setattr(node, "sync_users_chunked", sync, raising=False)
    try:
        await node.connect("0.5.4", "26.3.27")
        await node_operation.NodeOperation._resume_shared_sync(node)
        await asyncio.sleep(1)
        assert not node._sync_worker_task.done()
        sync.assert_not_awaited()
        now[0] += 61
        await asyncio.wait_for(node._sync_worker_task, timeout=5)
        assert [user.email for user in sync.await_args.args[0]] == ["orphan"]
        await wait_for_keys(store, "p.93.", 0)
    finally:
        await node.disconnect()
        await store.close()
