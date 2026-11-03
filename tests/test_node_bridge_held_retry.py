import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

import certifi
import pytest
from grpclib.client import Channel
from grpclib.server import Server
from PasarGuardNodeBridge.common import service_grpc, service_pb2 as service
from PasarGuardNodeBridge.storage import InMemoryUserSyncStore

from app.node import bridge as node_bridge
from app.node.bridge import QueuedGrpcNode
from app.node.nats_memory import NatsUserSyncStore
from tests.test_node_bridge_pin_nats_reject import nats_rejecting_world
from tests.test_node_reject_isolation import NODE_0620, RejectingNode

SETTLE_SECONDS = 20
HELD_RETRY_API = ("hold_users", "retire_held")


class HoldOnlyStore(InMemoryUserSyncStore):
    retire_held = None
    requeue_held_users = None

    def __init__(self):
        super().__init__()
        self.held_calls = []

    async def hold_users(self, node_id, tokens, retry_after=0.0):
        self.held_calls.append(list(tokens))


class RetireOnlyStore(InMemoryUserSyncStore):
    hold_users = None
    requeue_held_users = None

    def __init__(self):
        super().__init__()
        self.retired_calls = []

    async def retire_held(self, node_id, tokens):
        self.retired_calls.append(list(tokens))


class World:
    def __init__(self, receiver, node, store, database):
        self.receiver = receiver
        self.node = node
        self.store = store
        self.database = database
        self.claimed_tokens = {}

    async def write(self, email, inbound):
        self.database[email] = inbound
        await self.node.update_user(service.User(email=email, inbounds=["queued-payload"]))

    async def until(self, condition, timeout=SETTLE_SECONDS):
        async with asyncio.timeout(timeout):
            while not condition():
                await asyncio.sleep(0.01)

    async def drained(self, timeout=SETTLE_SECONDS):
        async with asyncio.timeout(timeout):
            while await self.store.has_pending(self.node.node_id):
                await asyncio.sleep(0.01)


@asynccontextmanager
async def rejecting_world_over(monkeypatch, store):
    receiver = RejectingNode(NODE_0620)
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
        node_id=f"held-{uuid4().hex[:8]}",
        user_sync_store=store,
        sync_poll_interval=0.01,
        sync_batch_size=100,
    )
    node.channel.close()
    node.channel = Channel("127.0.0.1", port, ssl=False)
    node._client = service_grpc.NodeServiceStub(node.channel)
    world = World(receiver, node, store, database)
    original_claim = store.claim_users

    async def recording_claim(node_id, worker_id, limit, lease_seconds):
        claimed = await original_claim(node_id, worker_id, limit, lease_seconds)
        for item in claimed:
            world.claimed_tokens.setdefault(item.user.email, []).append(item.token)
        return claimed

    store.claim_users = recording_claim
    try:
        yield world
    finally:
        await node.disconnect()
        node.channel.close()
        await node._json_client.close()
        server.close()
        await server.wait_closed()


def test_the_nats_store_exposes_the_held_retry_api_whole_or_not_at_all():
    present = [name for name in HELD_RETRY_API if callable(getattr(NatsUserSyncStore, name, None))]
    assert present in ([], list(HELD_RETRY_API))
    assert callable(getattr(NatsUserSyncStore, "request_refresh", None))


async def test_a_rejected_user_on_the_nats_store_is_refreshed_and_delivered_with_its_resolved_payload(monkeypatch):
    async with nats_rejecting_world(monkeypatch) as world:
        markers_written = []
        markers_resolved = []
        original_marker = world.store._request_refresh_one
        original_resolve = world.store._resolve_refresh_markers

        async def recording_marker(node_id, email):
            created = await original_marker(node_id, email)
            markers_written.append((email, created))
            return created

        async def recording_resolve(node_id, markers, worker_id, lease_seconds):
            markers_resolved.extend(markers)
            return await original_resolve(node_id, markers, worker_id, lease_seconds)

        world.store._request_refresh_one = recording_marker
        world.store._resolve_refresh_markers = recording_resolve

        await world.write("user-a", "a1")
        await world.write("user-b", "bad")
        await world.write("user-c", "c1")
        await world.node.connect(NODE_0620, "26.3.27")
        await world.until(lambda: "user-a" in world.receiver.applied and "user-c" in world.receiver.applied)
        assert "user-b" not in world.receiver.applied
        world.database["user-b"] = "b-resolved"
        await world.until(lambda: world.receiver.applied.get("user-b") == ["b-resolved"])
        await world.drained()
        assert "user-b" in world.refresh_requests
        assert ("user-b", True) in markers_written
        assert "user-b" in markers_resolved
        assert world.receiver.applied == {"user-a": ["a1"], "user-b": ["b-resolved"], "user-c": ["c1"]}


@pytest.mark.parametrize("store_class", [HoldOnlyStore, RetireOnlyStore])
async def test_a_store_with_only_half_the_held_retry_api_is_never_held_and_still_delivers(monkeypatch, store_class):
    store = store_class()
    async with rejecting_world_over(monkeypatch, store) as world:
        await world.write("user-a", "a1")
        await world.write("user-b", "bad")
        await world.write("user-c", "c1")
        await world.node.connect(NODE_0620, "26.3.27")
        await world.until(lambda: "user-a" in world.receiver.applied and "user-c" in world.receiver.applied)
        assert "user-b" not in world.receiver.applied
        world.database["user-b"] = "b-ok"
        await world.until(lambda: world.receiver.applied.get("user-b") == ["b-ok"])
        await world.drained()
        assert world.receiver.applied == {"user-a": ["a1"], "user-b": ["b-ok"], "user-c": ["c1"]}
        assert getattr(store, "held_calls", []) == []
        assert getattr(store, "retired_calls", []) == []


async def test_a_store_with_the_whole_held_retry_api_holds_the_rejected_user_and_retries_it(monkeypatch):
    for name in HELD_RETRY_API:
        assert callable(getattr(InMemoryUserSyncStore, name, None)), f"InMemoryUserSyncStore lacks {name}"
    store = InMemoryUserSyncStore()
    held = []
    original_hold = store.hold_users

    async def recording_hold(node_id, tokens, retry_after):
        held.append((list(tokens), retry_after))
        return await original_hold(node_id, tokens, retry_after)

    store.hold_users = recording_hold
    async with rejecting_world_over(monkeypatch, store) as world:
        await world.write("user-a", "a1")
        await world.write("user-b", "bad")
        await world.write("user-c", "c1")
        await world.node.connect(NODE_0620, "26.3.27")
        await world.until(lambda: "user-a" in world.receiver.applied and "user-c" in world.receiver.applied)
        await world.until(lambda: bool(held))
        assert "user-b" not in world.receiver.applied
        world.database["user-b"] = "b-ok"
        await world.until(lambda: world.receiver.applied.get("user-b") == ["b-ok"])
        await world.drained()
        held_tokens = {token for tokens, _ in held for token in tokens}
        assert held_tokens & set(world.claimed_tokens["user-b"])
        assert not held_tokens & set(world.claimed_tokens["user-a"] + world.claimed_tokens["user-c"])
        assert all(retry_after > 0 for _, retry_after in held)
        assert len(world.claimed_tokens["user-b"]) >= 2
        assert world.receiver.applied == {"user-a": ["a1"], "user-b": ["b-ok"], "user-c": ["c1"]}
