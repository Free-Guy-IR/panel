import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

import certifi
from grpclib.client import Channel
from grpclib.server import Server
from PasarGuardNodeBridge.common import service_grpc, service_pb2 as service

from app.nats.kv_cas import MemoryCasKv
from app.node import bridge as node_bridge
from app.node.bridge import QueuedGrpcNode
from app.node.nats_memory import NatsUserSyncStore
from tests.test_node_reject_isolation import NODE_0620, RejectingNode

SETTLE_SECONDS = 15


class NatsWorld:
    def __init__(self, receiver, node, store, database):
        self.receiver = receiver
        self.node = node
        self.store = store
        self.database = database
        self.refresh_requests = []

    async def write(self, email, inbound):
        self.database[email] = inbound
        await self.node.update_user(service.User(email=email, inbounds=["queued-payload"]))

    async def remove(self, email):
        self.database.pop(email, None)
        await self.node.update_user(service.User(email=email, inbounds=[]))

    async def until(self, condition, timeout=SETTLE_SECONDS):
        async with asyncio.timeout(timeout):
            while not condition():
                await asyncio.sleep(0.01)

    async def drained(self, timeout=SETTLE_SECONDS):
        async with asyncio.timeout(timeout):
            while await self.store.has_pending(self.node.node_id):
                await asyncio.sleep(0.01)


@asynccontextmanager
async def nats_rejecting_world(monkeypatch):
    receiver = RejectingNode(NODE_0620)
    server = Server([receiver])
    await server.start("127.0.0.1", 0)
    port = server._server.sockets[0].getsockname()[1]
    database = {}

    async def current_state(node_id, emails):
        return [service.User(email=email, inbounds=[database[email]] if email in database else []) for email in emails]

    monkeypatch.setattr(node_bridge, "_refresh_handler", current_state)
    store = NatsUserSyncStore(MemoryCasKv())
    node = QueuedGrpcNode(
        address="127.0.0.1",
        port=port,
        api_port=1,
        server_ca=certifi.contents(),
        api_key=str(uuid4()),
        node_id=f"nats-reject-{uuid4().hex[:8]}",
        user_sync_store=store,
        sync_poll_interval=0.01,
        sync_batch_size=100,
    )
    node.channel.close()
    node.channel = Channel("127.0.0.1", port, ssl=False)
    node._client = service_grpc.NodeServiceStub(node.channel)
    world = NatsWorld(receiver, node, store, database)
    original_request_refresh = store.request_refresh

    async def recording_request_refresh(node_id, emails):
        world.refresh_requests.extend(emails)
        return await original_request_refresh(node_id, emails)

    store.request_refresh = recording_request_refresh
    try:
        yield world
    finally:
        await node.disconnect()
        node.channel.close()
        await node._json_client.close()
        server.close()
        await server.wait_closed()


async def test_users_behind_a_rejected_user_on_the_nats_store_are_delivered_and_the_rejected_one_is_refreshed(
    monkeypatch,
):
    async with nats_rejecting_world(monkeypatch) as world:
        await world.write("user-a", "a1")
        await world.write("user-b", "bad")
        await world.write("user-c", "c1")
        await world.node.connect(NODE_0620, "26.3.27")
        await world.until(lambda: "user-a" in world.receiver.applied and "user-c" in world.receiver.applied)
        assert world.receiver.applied["user-a"] == ["a1"]
        assert world.receiver.applied["user-c"] == ["c1"]
        assert "user-b" not in world.receiver.applied
        await world.until(lambda: "user-b" in world.refresh_requests)

        await world.write("user-b", "b-fixed")
        await world.until(lambda: world.receiver.applied.get("user-b") == ["b-fixed"])
        await world.drained()
        assert world.receiver.applied == {"user-a": ["a1"], "user-b": ["b-fixed"], "user-c": ["c1"]}


async def test_concurrent_writes_while_a_user_is_rejected_on_the_nats_store_end_on_the_newest_state(monkeypatch):
    async with nats_rejecting_world(monkeypatch) as world:
        await world.write("user-a", "a1")
        await world.write("user-b", "bad")
        await world.write("user-c", "c1")

        async def writes_while_rejected(bad):
            if world.receiver.calls == 1:
                await world.write("user-b", "b2")
                await world.remove("user-c")

        world.receiver.on_reject = writes_while_rejected
        await world.node.connect(NODE_0620, "26.3.27")

        async def concurrent_writer(seed):
            for step in range(5):
                await world.write("user-b", f"b-{seed}-{step}")
                await asyncio.sleep(0.002 * (seed + 1))

        await asyncio.gather(*(concurrent_writer(seed) for seed in range(3)))
        newest = world.database["user-b"]
        await world.until(lambda: world.receiver.applied.get("user-b") == [newest])
        await world.until(lambda: world.receiver.applied.get("user-c") == [])
        await world.drained()
        assert world.receiver.applied.get("user-b") == [newest]
        assert world.receiver.applied.get("user-c") == []
        assert world.receiver.applied["user-a"] == ["a1"]
        assert "user-c" not in world.database
