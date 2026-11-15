from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import certifi
import pytest
from grpclib.client import Channel
from grpclib.const import Cardinality, Handler
from grpclib.server import Server
from PasarGuardNodeBridge.common import service_grpc, service_pb2 as service
from sqlalchemy import select, update

from app.db.crud.user import get_users_by_ids, remove_user as crud_remove_user
from app.db.models import Admin, AdminRole, AdminStatus, User, UserStatus
from app.jobs import record_usages
from app.models.admin import AdminDetails, AdminRoleData, BulkAdminSelection
from app.models.proxy import ProxyTable
from app.node import bridge as node_bridge, node_manager, sync as node_sync
from app.node.bridge import QueuedGrpcNode
from app.node.user import serialize_users_for_node
from app.operation import OperatorType, admin as admin_operation
from app.operation.admin import AdminOperation
from tests.a1_bridge_harness import isolate_node_manager
from tests.test_merge_interleavings_status_admin import _give_inbound
from tests.test_recorder_harness import file_backed_database_url, recorder_database, seed_users_and_nodes

OWNER = AdminDetails(id=None, username="owner", role=AdminRoleData(id=1, name="owner", is_owner=True))
BOUND = 30
NODE_ID = 9301


def _shape(proto) -> tuple:
    return proto.proxies.SerializeToString(deterministic=True), tuple(sorted(proto.inbounds))


class Receiver:
    def __init__(self):
        self.last: dict[str, tuple] = {}
        self.deliveries: list[tuple[str, tuple]] = []

    async def chunked(self, stream):
        for chunk in [chunk async for chunk in stream]:
            for user in chunk.users:
                self.last[user.email] = _shape(user)
                self.deliveries.append((user.email, tuple(sorted(user.inbounds))))
        await stream.send_message(service.Empty())

    def __mapping__(self):
        return {
            "/service.NodeService/SyncUsersChunked": Handler(
                self.chunked, Cardinality.STREAM_UNARY, service.UsersChunk, service.Empty
            )
        }


@pytest.fixture(name="recorder_db")
async def recorder_db_fixture(monkeypatch: pytest.MonkeyPatch, tmp_path):
    async with recorder_database(monkeypatch, file_backed_database_url(tmp_path)) as session_factory:
        yield session_factory


@pytest.fixture(name="node")
async def node_fixture(monkeypatch: pytest.MonkeyPatch, recorder_db):
    monkeypatch.setattr(node_sync, "GetDB", record_usages.GetDB)
    monkeypatch.setattr(node_sync, "_current_state", node_sync.CurrentStateReader(query_timeout=BOUND))
    monkeypatch.setattr(node_bridge, "_refresh_handler", node_sync.refresh_node_users)
    monkeypatch.setattr(admin_operation.notification, "modify_admin", AsyncMock())
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
        node_id=f"bulk-admin-{uuid4().hex[:8]}",
        sync_poll_interval=0.01,
    )
    node.channel.close()
    node.channel = Channel("127.0.0.1", port, ssl=False)
    node._client = service_grpc.NodeServiceStub(node.channel)
    await node.connect("0.6.22", "26.3.27")
    isolate_node_manager(monkeypatch, node_manager, {NODE_ID: node})
    node.receiver = receiver
    try:
        yield node
    finally:
        await node.disconnect()
        node.channel.close()
        await node._json_client.close()
        server.close()
        await server.wait_closed()


async def _delivered(node) -> None:
    async with asyncio.timeout(BOUND):
        while True:
            await asyncio.sleep(0.1)
            if not await node._user_sync_store.has_pending(node.node_id):
                await asyncio.sleep(0.2)
                if not await node._user_sync_store.has_pending(node.node_id):
                    return


async def _seed_blocked_admin(recorder_db, user_count: int) -> tuple[int, list[int]]:
    admin_id, user_ids, _ = await seed_users_and_nodes(recorder_db, user_count=user_count, node_count=0)
    await _give_inbound(recorder_db, user_ids)
    async with recorder_db() as session:
        for offset, user_id in enumerate(user_ids):
            await session.execute(
                update(User).where(User.id == user_id).values(status=UserStatus.active, hwid_limit=3 + offset)
            )
        await session.execute(
            update(AdminRole)
            .where(AdminRole.id == 3)
            .values(disconnect_users_when_limited=True, disconnect_users_when_disabled=True)
        )
        await session.execute(update(Admin).where(Admin.id == admin_id).values(status=AdminStatus.disabled))
        await session.commit()
    return admin_id, user_ids


async def _committed_payloads(recorder_db, user_ids: list[int]) -> dict[str, tuple]:
    async with recorder_db() as session:
        users = await get_users_by_ids(session, user_ids, load_admin_role=True)
        protos = await serialize_users_for_node(users)
    return {proto.email: _shape(proto) for proto in protos}


class BulkDispatchRace:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, race):
        self._real = node_sync._dispatch_users_update
        self._race = race
        self.stale: dict[str, object] = {}
        self.armed = True
        self.done = asyncio.Event()
        monkeypatch.setattr(node_sync, "_dispatch_users_update", self._dispatch_after_the_race)

    async def _dispatch_after_the_race(self, proto_users, **kwargs):
        if not self.armed:
            return await self._real(proto_users, **kwargs)
        self.armed = False
        self.stale = {proto.email: proto for proto in proto_users}
        try:
            await self._race()
            return await self._real(proto_users, **kwargs)
        finally:
            self.done.set()


async def _bulk_enable(admin_id: int) -> None:
    async with record_usages.GetDB() as db:
        await AdminOperation(OperatorType.API).bulk_set_admins_disabled(
            db, BulkAdminSelection(ids={admin_id}), OWNER, is_disabled=False
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("racing_status", [UserStatus.active, UserStatus.disabled])
async def test_a_user_modified_and_pushed_while_a_bulk_enable_pushes_its_older_read_ends_in_its_committed_state(
    monkeypatch: pytest.MonkeyPatch, recorder_db, node, racing_status
):
    admin_id, user_ids = await _seed_blocked_admin(recorder_db, 3)
    racing = user_ids[1]

    async def modify_push_and_deliver_first():
        async with recorder_db() as session:
            await session.execute(
                update(User)
                .where(User.id == racing)
                .values(status=racing_status, hwid_limit=41, proxy_settings=ProxyTable().dict(no_obj=True))
            )
            await session.commit()
        async with recorder_db() as session:
            fresh = await get_users_by_ids(session, [racing], load_admin_role=True)
        await node_sync.sync_users(fresh, await_dispatch=True)
        await _delivered(node)

    race = BulkDispatchRace(monkeypatch, modify_push_and_deliver_first)
    await _bulk_enable(admin_id)
    await asyncio.wait_for(race.done.wait(), BOUND)
    await _delivered(node)

    expected = await _committed_payloads(recorder_db, user_ids)
    assert _shape(race.stale[str(racing)]) != expected[str(racing)], "the bulk push did not carry an older read"
    stale = sorted(email for email in expected if node.receiver.last.get(email) != expected[email])
    assert stale == [], (racing_status.value, stale, node.receiver.deliveries)


@pytest.mark.asyncio
async def test_a_user_deleted_while_a_bulk_enable_pushes_its_older_read_is_not_put_back_on_the_node(
    monkeypatch: pytest.MonkeyPatch, recorder_db, node
):
    admin_id, user_ids = await _seed_blocked_admin(recorder_db, 3)
    victim = user_ids[0]

    async def delete_remove_and_deliver_first():
        async with recorder_db() as session:
            doomed = await get_users_by_ids(session, [victim], load_admin_role=True)
            db_user = (await session.execute(select(User).where(User.id == victim))).scalar_one()
            await crud_remove_user(session, db_user)
            await session.commit()
        await node_sync.remove_users(doomed, await_dispatch=True)
        await _delivered(node)

    race = BulkDispatchRace(monkeypatch, delete_remove_and_deliver_first)
    await _bulk_enable(admin_id)
    await asyncio.wait_for(race.done.wait(), BOUND)
    await _delivered(node)

    assert list(race.stale[str(victim)].inbounds), "the bulk push did not carry the deleted user with inbounds"
    async with recorder_db() as session:
        assert (await session.execute(select(User.id).where(User.id == victim))).first() is None
    victim_deliveries = [inbounds for email, inbounds in node.receiver.deliveries if email == str(victim)]
    assert victim_deliveries and victim_deliveries[-1] == (), node.receiver.deliveries
    expected = await _committed_payloads(recorder_db, user_ids[1:])
    stale = sorted(email for email in expected if node.receiver.last.get(email) != expected[email])
    assert stale == [], (stale, node.receiver.deliveries)
