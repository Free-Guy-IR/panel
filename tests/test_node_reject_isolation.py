import asyncio
import json
import random
from contextlib import asynccontextmanager
from uuid import uuid4

import certifi
import pytest
from grpclib.client import Channel
from grpclib.const import Cardinality, Handler, Status
from grpclib.exceptions import GRPCError
from grpclib.server import Server
from PasarGuardNodeBridge.common import service_grpc, service_pb2 as service
from PasarGuardNodeBridge.storage import InMemoryUserSyncStore

from app.node import bridge as node_bridge
from app.node.bridge import QueuedGrpcNode

NODE_0622 = "0.6.22"
NODE_0620 = "0.6.20"


class RejectingNode:
    def __init__(self, contract):
        self.contract = contract
        self.applied = {}
        self.calls = 0
        self.rejected = []
        self.on_reject = None

    async def receive(self, stream):
        users = [user for chunk in [chunk async for chunk in stream] for user in chunk.users]
        self.calls += 1
        bad = [user.email for user in users if "bad" in user.inbounds]
        if bad and self.on_reject is not None:
            await self.on_reject(bad)
        if bad and self.contract == NODE_0620:
            raise GRPCError(Status.INTERNAL, f"failed to update users: invalid account {bad[0]}")
        for user in users:
            if user.email in bad:
                self.applied.pop(user.email, None)
            else:
                self.applied[user.email] = list(user.inbounds)
        self.rejected.extend(bad)
        await stream.send_initial_metadata(metadata={"pg-capabilities": "keepalive-policy-v1,rejected-users-v1"})
        await stream.send_message(service.Empty())
        report = [{"email": email, "backend": "xray", "reason": "invalid account"} for email in bad]
        await stream.send_trailing_metadata(metadata={"pg-rejected-users": json.dumps(report)} if report else None)

    def __mapping__(self):
        return {
            "/service.NodeService/SyncUsersChunked": Handler(
                self.receive, Cardinality.STREAM_UNARY, service.UsersChunk, service.Empty
            )
        }


class Harness:
    def __init__(self, receiver, node, database):
        self.receiver = receiver
        self.node = node
        self.database = database

    async def write(self, email, inbound):
        self.database[email] = inbound
        await self.node.update_user(service.User(email=email, inbounds=["queued-payload"]))

    async def drained(self, timeout=15):
        async with asyncio.timeout(timeout):
            while await self.node._user_sync_store.has_pending(self.node.node_id):
                await asyncio.sleep(0.01)


@asynccontextmanager
async def rejecting_world(monkeypatch, contract, batch_size=100):
    receiver = RejectingNode(contract)
    server = Server([receiver])
    await server.start("127.0.0.1", 0)
    port = server._server.sockets[0].getsockname()[1]
    database = {}

    async def current_state(node_id, emails):
        return [service.User(email=email, inbounds=[database[email]]) for email in emails]

    monkeypatch.setattr(node_bridge, "_refresh_handler", current_state)
    node = QueuedGrpcNode(
        address="127.0.0.1",
        port=port,
        api_port=1,
        server_ca=certifi.contents(),
        api_key=str(uuid4()),
        node_id=f"reject-{uuid4().hex[:8]}",
        user_sync_store=InMemoryUserSyncStore(),
        sync_poll_interval=0.01,
        sync_batch_size=batch_size,
    )
    node.channel.close()
    node.channel = Channel("127.0.0.1", port, ssl=False)
    node._client = service_grpc.NodeServiceStub(node.channel)
    try:
        yield Harness(receiver, node, database)
    finally:
        await node.disconnect()
        node.channel.close()
        await node._json_client.close()
        server.close()
        await server.wait_closed()


async def test_one_user_a_0622_node_rejects_does_not_block_the_rest_of_the_panel_queue(monkeypatch):
    async with rejecting_world(monkeypatch, NODE_0622) as world:
        for index in range(30):
            await world.write(f"user-{index}", "bad" if index == 17 else f"v{index}")
        await world.node.connect(NODE_0622, "26.3.27")
        await world.drained()
        assert world.receiver.applied == {f"user-{index}": [f"v{index}"] for index in range(30) if index != 17}
        assert world.receiver.rejected == ["user-17"]
        assert world.node._user_sync_failure_count == 0


async def test_a_database_fix_made_while_a_0622_node_rejects_the_user_is_delivered_next(monkeypatch):
    async with rejecting_world(monkeypatch, NODE_0622) as world:

        async def fix_in_database(bad):
            await world.write("user-9", "fixed")

        world.receiver.on_reject = fix_in_database
        for index in range(20):
            await world.write(f"user-{index}", "bad" if index == 9 else "v0")
        await world.node.connect(NODE_0622, "26.3.27")
        await world.drained()
        assert world.receiver.applied["user-9"] == ["fixed"]


async def test_concurrent_writers_against_a_0622_node_converge_to_the_database(monkeypatch):
    for round_number in range(12):
        rng = random.Random(round_number)
        async with rejecting_world(monkeypatch, NODE_0622, batch_size=rng.choice((5, 16, 50))) as world:
            emails = [f"user-{index}" for index in range(25)]
            for email in emails:
                await world.write(email, "bad" if rng.random() < 0.1 else "v0")
            await world.node.connect(NODE_0622, "26.3.27")

            async def writer(seed, world=world, emails=emails):
                local = random.Random(seed)
                for step in range(12):
                    await world.write(local.choice(emails), "bad" if local.random() < 0.15 else f"v{seed}-{step}")
                    await asyncio.sleep(local.choice((0, 0.001, 0.003)))

            await asyncio.gather(*(writer(round_number * 10 + index) for index in range(4)))
            await world.drained(timeout=20)
            good = {email: [inbound] for email, inbound in world.database.items() if inbound != "bad"}
            bad = {email for email, inbound in world.database.items() if inbound == "bad"}
            assert {email: world.receiver.applied.get(email) for email in good} == good, round_number
            assert bad.isdisjoint(world.receiver.applied), round_number
            assert not world.node.requires_hard_reset()


@pytest.mark.xfail(
    strict=True,
    reason="waits for node 0.6.22: a node before it fails the whole chunk for one invalid user, and the "
    "upstream-identical bridge retries that chunk as upstream does",
)
async def test_one_user_a_0620_node_rejects_stalls_its_batch_until_the_node_reports_it(monkeypatch):
    async with rejecting_world(monkeypatch, NODE_0620) as world:
        for index in range(10):
            await world.write(f"user-{index}", "bad" if index == 3 else f"v{index}")
        await world.node.connect(NODE_0620, "26.3.27")
        await world.drained(timeout=5)
