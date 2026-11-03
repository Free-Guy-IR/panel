import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

import certifi
import pytest
from grpclib.client import Channel
from grpclib.const import Cardinality, Handler, Status
from grpclib.exceptions import GRPCError
from grpclib.server import Server
from PasarGuardNodeBridge import NodeAPIError
from PasarGuardNodeBridge.common import service_grpc, service_pb2 as service
from PasarGuardNodeBridge.storage import InMemoryUserSyncStore

from app.nats.kv_cas import MemoryCasKv
from app.node import NodeManager, bridge as node_bridge
from app.node.bridge import QueuedGrpcNode
from app.node.nats_memory import NatsUserSyncStore
from tests.test_node_reject_isolation import NODE_0620

SETTLE_SECONDS = 20
NODE_KEY = 1
REJECTED = ("user-b1", "user-b2", "user-b3")
ACCEPTED = {"user-a": "a1", "user-c": "c1"}


class SnapshotNode:
    def __init__(self):
        self.applied = {}
        self.deliveries = []
        self.snapshots = []

    def _apply(self, kind, users):
        self.deliveries.append((kind, {user.email: list(user.inbounds) for user in users}))
        bad = [user.email for user in users if "bad" in user.inbounds]
        if bad:
            raise GRPCError(Status.INTERNAL, f"failed to update users: invalid account {bad[0]}")
        if kind == "snapshot":
            self.applied = {}
            self.snapshots.append(len(self.deliveries) - 1)
        for user in users:
            self.applied[user.email] = list(user.inbounds)

    async def chunked(self, stream):
        users = [user for chunk in [chunk async for chunk in stream] for user in chunk.users]
        self._apply("delta", users)
        await stream.send_initial_metadata(metadata={"pg-capabilities": "keepalive-policy-v1,rejected-users-v1"})
        await stream.send_message(service.Empty())

    async def snapshot(self, stream):
        request = await stream.recv_message()
        self._apply("snapshot", list(request.users))
        await stream.send_message(service.Empty())

    def sent_after_last_snapshot(self, email):
        start = self.snapshots[-1] + 1
        return [users[email] for kind, users in self.deliveries[start:] if email in users]

    def __mapping__(self):
        return {
            "/service.NodeService/SyncUsersChunked": Handler(
                self.chunked, Cardinality.STREAM_UNARY, service.UsersChunk, service.Empty
            ),
            "/service.NodeService/SyncUsers": Handler(
                self.snapshot, Cardinality.UNARY_UNARY, service.Users, service.Empty
            ),
        }


class World:
    def __init__(self, receiver, node, store, database, manager):
        self.receiver = receiver
        self.node = node
        self.store = store
        self.database = database
        self.manager = manager

    async def write(self, email, inbound):
        self.database[email] = inbound
        await self.node.update_user(service.User(email=email, inbounds=["queued-payload"]))

    async def until(self, condition, timeout=SETTLE_SECONDS):
        async with asyncio.timeout(timeout):
            while not condition():
                await asyncio.sleep(0.01)

    async def settled(self, timeout=SETTLE_SECONDS):
        async with asyncio.timeout(timeout):
            while True:
                queued, active = await self.store.capture_queued(self.node.node_id)
                if not queued and not active:
                    return
                await asyncio.sleep(0.01)

    async def snapshot_users(self):
        return [service.User(email=email, inbounds=[inbound]) for email, inbound in sorted(self.database.items())]

    async def full_sync(self):
        return await self.manager.sync_full(NODE_KEY, self.snapshot_users, flush_pending=True)

    async def reject_and_hold(self):
        for email, inbound in ACCEPTED.items():
            await self.write(email, inbound)
        for email in REJECTED:
            await self.write(email, "bad")
        await self.node.connect(NODE_0620, "26.3.27")
        await self.until(
            lambda: all(self.receiver.applied.get(email) == [inbound] for email, inbound in ACCEPTED.items())
        )
        await self.until(lambda: set(REJECTED) <= self.node._sync_retries.keys())
        assert not set(REJECTED) & self.receiver.applied.keys()


def memory_store():
    return InMemoryUserSyncStore()


def nats_store():
    return NatsUserSyncStore(MemoryCasKv())


@asynccontextmanager
async def snapshot_world(monkeypatch, store, retry_delays):
    receiver = SnapshotNode()
    server = Server([receiver])
    await server.start("127.0.0.1", 0)
    port = server._server.sockets[0].getsockname()[1]
    database = {}

    async def current_state(node_id, emails):
        return [service.User(email=email, inbounds=[database[email]] if email in database else []) for email in emails]

    monkeypatch.setattr(node_bridge, "_refresh_handler", current_state)
    node = QueuedGrpcNode(
        address="127.0.0.1",
        port=port,
        api_port=1,
        server_ca=certifi.contents(),
        api_key=str(uuid4()),
        node_id=f"snapshot-held-{uuid4().hex[:8]}",
        user_sync_store=store,
        sync_poll_interval=0.01,
        sync_batch_size=2,
    )
    node._sync_retry_delays = retry_delays
    node.channel.close()
    node.channel = Channel("127.0.0.1", port, ssl=False)
    node._client = service_grpc.NodeServiceStub(node.channel)
    manager = NodeManager()
    manager._nodes[NODE_KEY] = node
    try:
        yield World(receiver, node, store, database, manager)
    finally:
        await node.disconnect()
        node.channel.close()
        await node._json_client.close()
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize("make_store", [memory_store, nats_store])
async def test_a_snapshot_taken_while_rejected_users_are_held_retires_their_retries_and_sends_nothing_stale(
    monkeypatch, make_store
):
    async with snapshot_world(monkeypatch, make_store(), (60.0, 60.0, 60.0)) as world:
        await world.reject_and_hold()
        queued, _ = await world.store.capture_queued(world.node.node_id)
        if isinstance(world.store, InMemoryUserSyncStore):
            assert set(REJECTED) <= queued.keys()
        for email in REJECTED:
            world.database[email] = f"{email}-ok"

        assert await world.full_sync() is world.node

        expected = {
            **{email: [inbound] for email, inbound in ACCEPTED.items()},
            **{email: [f"{email}-ok"] for email in REJECTED},
        }
        assert world.receiver.applied == expected
        assert not set(REJECTED) & world.node._sync_retries.keys()
        await world.settled()
        await world.write("user-d", "d1")
        await world.until(lambda: world.receiver.applied.get("user-d") == ["d1"])
        await world.settled()
        for email in REJECTED:
            assert all(payload == [f"{email}-ok"] for payload in world.receiver.sent_after_last_snapshot(email))
        assert world.receiver.applied == {**expected, "user-d": ["d1"]}


@pytest.mark.parametrize("make_store", [memory_store, nats_store])
async def test_a_write_racing_the_snapshot_of_a_held_user_survives_the_retire_and_lands_last(monkeypatch, make_store):
    async with snapshot_world(monkeypatch, make_store(), (60.0, 60.0, 60.0)) as world:
        await world.reject_and_hold()
        for email in REJECTED:
            world.database[email] = f"{email}-ok"
        original_sync = world.node.sync_users
        raced = []

        async def racing_sync(users, flush_pending=False, timeout=None):
            if not raced:
                raced.append(True)
                await world.write("user-b2", "b2-new")
            return await original_sync(users, flush_pending=flush_pending, timeout=timeout)

        world.node.sync_users = racing_sync
        assert await world.full_sync() is world.node
        assert raced
        snapshot = world.receiver.deliveries[world.receiver.snapshots[-1]][1]
        assert snapshot["user-b2"] == ["user-b2-ok"]

        await world.until(lambda: world.receiver.applied.get("user-b2") == ["b2-new"])
        await world.settled()
        assert world.receiver.sent_after_last_snapshot("user-b2")[-1] == ["b2-new"]
        assert world.receiver.applied == {
            "user-a": ["a1"],
            "user-b1": ["user-b1-ok"],
            "user-b2": ["b2-new"],
            "user-b3": ["user-b3-ok"],
            "user-c": ["c1"],
        }


@pytest.mark.parametrize("make_store", [memory_store, nats_store])
async def test_a_snapshot_the_node_refuses_keeps_the_held_retries_and_they_deliver_once_fixed(monkeypatch, make_store):
    async with snapshot_world(monkeypatch, make_store(), (0.5, 0.5, 0.5)) as world:
        await world.reject_and_hold()

        with pytest.raises(NodeAPIError):
            await world.full_sync()

        assert not world.receiver.snapshots
        assert set(REJECTED) <= world.node._sync_retries.keys()
        if isinstance(world.store, InMemoryUserSyncStore):
            queued, _ = await world.store.capture_queued(world.node.node_id)
            assert set(REJECTED) <= queued.keys()
        for email in REJECTED:
            world.database[email] = f"{email}-ok"
        await world.until(lambda: all(world.receiver.applied.get(email) == [f"{email}-ok"] for email in REJECTED))
        await world.settled()
        assert world.receiver.applied == {
            **{email: [inbound] for email, inbound in ACCEPTED.items()},
            **{email: [f"{email}-ok"] for email in REJECTED},
        }
