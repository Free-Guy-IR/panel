import asyncio
import time
from uuid import uuid4

import certifi
import nats.js.errors as nats_js_errors
import pytest
from PasarGuardNodeBridge.storage import LifecycleOperation

from app.nats.kv_cas import MemoryCasKv, kv_get_json
from app.node import nats_memory
from app.node.bridge import QueuedGrpcNode
from app.node.nats_memory import NatsNodeLifecycleCoordinator

LEASE_SECONDS = 0.3


class WriteOutageKv(MemoryCasKv):
    def __init__(self):
        super().__init__()
        self.writes_fail = False

    async def update(self, key, value, last=None):
        if self.writes_fail:
            raise nats_js_errors.APIError(code=503, description="jetstream write unavailable")
        return await super().update(key, value, last)


@pytest.fixture(autouse=True)
def instant_cas_backoff(monkeypatch):
    async def no_wait():
        await asyncio.sleep(0)

    monkeypatch.setattr(nats_memory, "cas_retry_backoff", no_wait)


async def test_a_heartbeat_that_cannot_write_does_not_report_a_still_owned_lease_as_lost():
    kv = WriteOutageKv()
    coordinator = NatsNodeLifecycleCoordinator(kv)
    lease = await coordinator.try_acquire("1", "worker-a", LifecycleOperation.UPDATE_NODE, 30)
    assert lease is not None

    kv.writes_fail = True
    assert await coordinator.heartbeat(lease) is not False


async def test_a_heartbeat_for_a_lease_another_worker_took_still_reports_it_lost():
    kv = WriteOutageKv()
    coordinator = NatsNodeLifecycleCoordinator(kv)
    stale = await coordinator.try_acquire("1", "worker-a", LifecycleOperation.UPDATE_NODE, 0)
    newer = await coordinator.try_acquire("1", "worker-b", LifecycleOperation.HARD_RESET, 30)
    assert stale is not None and newer is not None

    assert await coordinator.heartbeat(stale) is False
    kv.writes_fail = True
    assert await coordinator.heartbeat(stale) is False


async def test_a_lease_whose_writes_fail_for_longer_than_the_lease_is_renewed_again_once_writes_recover():
    kv = WriteOutageKv()
    coordinator = NatsNodeLifecycleCoordinator(kv)
    node = QueuedGrpcNode(
        address="127.0.0.1",
        port=1,
        api_port=1,
        server_ca=certifi.contents(),
        api_key=str(uuid4()),
        node_id=f"outage-{uuid4().hex[:8]}",
        lifecycle_coordinator=coordinator,
        lifecycle_lease_seconds=LEASE_SECONDS,
    )
    try:
        lease = await node._acquire_lifecycle_lease(LifecycleOperation.UPDATE_NODE)
        kv.writes_fail = True
        await asyncio.sleep(LEASE_SECONDS * 3)
        kv.writes_fail = False
        await asyncio.sleep(LEASE_SECONDS * 2)

        heartbeat = node._lifecycle_heartbeat_tasks.get(lease.token)
        assert heartbeat is not None and not heartbeat.done()
        doc, _ = await kv_get_json(kv, coordinator._key(lease.node_id))
        assert doc["lease"]["token"] == lease.token
        assert float(doc["lease"]["expires_at"]) > time.time()
        assert await coordinator.try_acquire(lease.node_id, "worker-b", LifecycleOperation.HARD_RESET, 30) is None
        await node._release_lifecycle_lease(lease)
    finally:
        for task in list(node._lifecycle_heartbeat_tasks.values()):
            if task is not None:
                task.cancel()
        node._lifecycle_heartbeat_tasks.clear()
        node.channel.close()
        await node._json_client.close()
