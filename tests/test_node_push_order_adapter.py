import asyncio
import random
from datetime import UTC, datetime as dt, timedelta as td
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import certifi
import pytest
from grpclib.client import Channel
from grpclib.const import Cardinality, Handler
from grpclib.server import Server
from PasarGuardNodeBridge.common import service_grpc, service_pb2 as service
from sqlalchemy import select, update

from app import notification
from app.db.models import User, UserStatus
from app.fork.jobs import connection_limiter
from app.fork.models.connection import ConnectionRestriction
from app.models.settings import ConnectionLimit
from app.models.user import BulkUsersSelection, UserModify
from app.node import node_manager, sync as node_sync
from app.node.bridge import QueuedGrpcNode
from app.operation import OperatorType
from app.operation.user import UserOperation
from tests.a1_bridge_harness import TestGetDB, isolate_node_manager, seed_group, seed_user
from tests.api import TestSession
from tests.api.helpers import unique_name

OWNER = SimpleNamespace(username="owner", is_owner=True, role=None, id=None)
LIMITS = ConnectionLimit(
    enabled=True,
    monitor_only=False,
    enforcement_enabled=True,
    persistence_cycles=1,
    punishment_steps=[30],
)


class Receiver:
    def __init__(self):
        self.live = {}
        self.deliveries = []

    async def chunked(self, stream):
        users = [user for chunk in [chunk async for chunk in stream] for user in chunk.users]
        for user in users:
            self.deliveries.append((user.email, sorted(user.inbounds)))
            if user.inbounds:
                self.live[user.email] = sorted(user.inbounds)
            else:
                self.live.pop(user.email, None)
        await stream.send_message(service.Empty())

    def __mapping__(self):
        return {
            "/service.NodeService/SyncUsersChunked": Handler(
                self.chunked, Cardinality.STREAM_UNARY, service.UsersChunk, service.Empty
            )
        }


async def _admin_enables(operator, user_id):
    async with TestSession() as db:
        await operator.modify_user_by_id(db, user_id, UserModify(status=UserStatus.active), OWNER)


async def _limiter_releases(operator, user_id):
    async with TestGetDB() as db:
        await db.execute(
            update(ConnectionRestriction)
            .where(ConnectionRestriction.user_id == user_id, ConnectionRestriction.active.is_(True))
            .values(restore_at=dt.now(UTC) - td(seconds=1))
        )
        await db.commit()
        await connection_limiter._release_due(db, LIMITS)


async def _disabled_by_admin(user_id):
    return None


async def _disabled_by_limiter(user_id):
    observation = SimpleNamespace(
        user_id=user_id,
        verdict="over_limit",
        streak=5,
        devices=4,
        limit_applied=2,
        details={},
        reasons=["push-order"],
    )
    async with TestGetDB() as db:
        await connection_limiter._enforce(db, [observation], LIMITS)


async def _drained(node):
    async with asyncio.timeout(10):
        while await node._user_sync_store.has_pending(node.node_id):
            await asyncio.sleep(0.02)


@pytest.mark.parametrize(
    ("initial_status", "disable_first", "older_push"),
    [
        (UserStatus.disabled, _disabled_by_admin, _admin_enables),
        (UserStatus.active, _disabled_by_limiter, _limiter_releases),
    ],
    ids=["admin-enable", "limiter-release"],
)
async def test_an_older_single_push_dispatched_late_never_overrides_a_newer_bulk_disable(
    monkeypatch, initial_status, disable_first, older_push
):
    monkeypatch.setattr(UserOperation, "generate_subscription_url", staticmethod(AsyncMock(return_value="")))
    for name in ("user_status_change", "modify_user"):
        monkeypatch.setattr(notification, name, AsyncMock())
    monkeypatch.setattr(node_sync, "GetDB", TestGetDB)
    monkeypatch.setattr(connection_limiter, "GetDB", TestGetDB)
    real_single = node_sync._dispatch_user_update
    operator = UserOperation(OperatorType.API)
    failures = []
    for round_number in range(10):
        rng = random.Random(round_number)
        receiver = Receiver()
        server = Server([receiver])
        await server.start("127.0.0.1", 0)
        port = server._server.sockets[0].getsockname()[1]
        node = QueuedGrpcNode(
            address="127.0.0.1",
            port=port,
            api_port=1,
            server_ca=certifi.contents(),
            api_key=str(uuid4()),
            node_id=f"order-{uuid4().hex[:8]}",
            sync_poll_interval=rng.choice((0.01, 0.2)),
        )
        node.channel.close()
        node.channel = Channel("127.0.0.1", port, ssl=False)
        node._client = service_grpc.NodeServiceStub(node.channel)
        try:
            await node.connect("0.6.22", "26.3.27")
            isolate_node_manager(monkeypatch, node_manager, {4100 + round_number: node})
            group_id = await seed_group(unique_name("order"))
            user_id = await seed_user(group_id, status=initial_status)
            await disable_first(user_id)
            await asyncio.sleep(0.1)
            await _drained(node)
            delay = rng.choice((0.05, 0.3, 0.6))

            async def slow_single(proto_user, delay=delay):
                await asyncio.sleep(delay)
                await real_single(proto_user)

            monkeypatch.setattr(node_sync, "_dispatch_user_update", slow_single)
            await older_push(operator, user_id)
            await asyncio.sleep(rng.choice((0, 0.01, 0.1)))
            async with TestSession() as db:
                await operator.bulk_disable_users(db, BulkUsersSelection(ids={user_id}), OWNER)
            await asyncio.sleep(delay + 1.0)
            await _drained(node)
            async with TestSession() as session:
                status = (await session.execute(select(User.status).where(User.id == user_id))).scalar_one()
            if status != UserStatus.disabled:
                failures.append(f"round {round_number}: user {user_id} ended {status.value}, not disabled")
            elif str(user_id) in receiver.live:
                failures.append(f"round {round_number}: disabled user {user_id} is served: {receiver.deliveries}")
        finally:
            monkeypatch.setattr(node_sync, "_dispatch_user_update", real_single)
            await node.disconnect()
            node.channel.close()
            await node._json_client.close()
            server.close()
            await server.wait_closed()
    assert failures == []
