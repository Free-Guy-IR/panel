import asyncio
import datetime
import functools
import itertools
import json
import random
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiorwlock import RWLock
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from PasarGuardNodeBridge import GrpcNode, Health, NodeAPIError
from PasarGuardNodeBridge.common import service_pb2 as service
from sqlalchemy import update

import app.node as app_node
from app.core.manager import core_manager
from app.db.models import (
    Group,
    NodeConnectionType,
    NodeStatus,
    ProxyInbound,
    User,
    UserStatus,
    users_groups_association,
)
from app.models.core import CoreType
from app.models.proxy import ProxyTable
from app.node import manager_sync, node_manager, sync as node_sync
from app.node.user import _serialize_user_for_node
from app.operation import OperatorType, node as node_operation
from app.operation.node import NodeOperation
from tests.api import GetTestDB, TestSession
from tests.api.helpers import unique_name

NODE_VERSION = "0.9.0"
CORE_VERSION = "26.3.27"
SETTINGS = ProxyTable().dict(no_obj=True)
BACKEND_TYPES = {
    CoreType.xray: service.BackendType.XRAY,
    CoreType.mtproto: service.BackendType.MTPROTO,
}
_NODE_IDS = itertools.count(60000 + random.randrange(0, 30000) * 10)


@functools.cache
def _ca_pem() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "node.test")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode()


async def eventually(predicate, timeout: float = 4.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        if predicate():
            return True
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.02)


class Remote:
    def __init__(self):
        self.running = False
        self.backends: dict[int, dict] = {}
        self.before: dict[str, list] = {}
        self.stream_fails = False
        self.stream_attempts = 0
        self.lost: set[str] = set()

    def on(self, rpc: str, hook) -> None:
        self.before.setdefault(rpc, []).append(hook)

    async def _fire(self, rpc: str) -> None:
        for hook in self.before.pop(rpc, []):
            await hook()

    @staticmethod
    def _members(users, tags) -> dict[str, tuple[str, ...]]:
        members = {}
        for user in users:
            inbounds = tuple(sorted(tag for tag in user.inbounds if tag in tags))
            if inbounds:
                members[user.email] = inbounds
        return members

    def _backend(self, request) -> dict:
        tags = set(json.loads(request.config)["tags"])
        return {"tags": tags, "users": self._members(request.users, tags)}

    def _apply_each(self, users) -> None:
        for backend in self.backends.values():
            for user in users:
                members = self._members([user], backend["tags"])
                if members:
                    backend["users"].update(members)
                else:
                    backend["users"].pop(user.email, None)

    def carries(self, email: str, backend_type=None) -> bool:
        return any(
            email in backend["users"]
            for kind, backend in self.backends.items()
            if backend_type is None or kind == backend_type
        )

    def info(self):
        return service.BaseInfoResponse(started=self.running, node_version=NODE_VERSION, core_version=CORE_VERSION)

    async def rpc(self, name: str, request):
        await asyncio.sleep(0)
        await self._fire(name)
        if name in self.lost:
            raise NodeAPIError(-1, "Request timed out")
        if name == "Start":
            self.running = True
            self.backends = {request.type: self._backend(request)}
            return self.info()
        if name == "Stop":
            self.running = False
            self.backends = {}
            return service.Empty()
        if name == "GetBaseInfo":
            return self.info()
        if not self.running:
            raise NodeAPIError(503, "core is not started yet")
        if name == "SyncUsers":
            for backend in self.backends.values():
                backend["users"] = self._members(request.users, backend["tags"])
            return service.Empty()
        if name == "AddBackend":
            self.backends[request.type] = self._backend(request)
            return service.Empty()
        if name == "RemoveBackend":
            self.backends.pop(request.type, None)
            return service.Empty()
        if name == "ListBackends":
            return service.BackendList(types=list(self.backends))
        raise AssertionError(f"unexpected RPC {name}")

    async def stream(self, users) -> list:
        self.stream_attempts += 1
        await self._fire("SyncUser")
        if not self.running or self.stream_fails:
            return list(users)
        self._apply_each(users)
        return []

    async def chunked(self, users) -> list:
        self.stream_attempts += 1
        await self._fire("SyncUser")
        if not self.running or self.stream_fails:
            return list(users)
        self._apply_each(users)
        return []


class ModelTransport(GrpcNode):
    def __init__(self, remote: Remote, **kwargs):
        super().__init__(**kwargs)
        self.remote = remote

    async def _handle_grpc_request(self, method, request, timeout=None):
        return await self.remote.rpc(method.name.rsplit("/", 1)[-1], request)

    async def _sync_batch_users(self, users):
        return await self.remote.stream(users)

    async def sync_users_chunked(self, users, chunk_size=100, flush_pending=False, timeout=None):
        if flush_pending:
            await self.flush_pending_users()
        return await self.remote.chunked(users)


def _model_class(real_class: type) -> type:
    if real_class is GrpcNode:
        return ModelTransport
    adapters = tuple(base for base in real_class.__bases__ if base is not GrpcNode)
    return type("ModelNode", (*adapters, ModelTransport), {})


def _core(core_type: CoreType, tags: list[str]):
    return SimpleNamespace(
        type=core_type,
        inbounds=list(tags),
        protocols=frozenset(),
        exclude_inbound_tags=[],
        to_str=lambda: json.dumps({"tags": list(tags)}),
    )


class Harness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch):
        self.monkeypatch = monkeypatch
        self.remotes: dict[int, Remote] = {}
        self.db_nodes: dict[int, SimpleNamespace] = {}
        self.tag_a = unique_name("snap_a")
        self.tag_b = unique_name("snap_b")
        self.cores = {1: _core(CoreType.xray, [self.tag_a]), 2: _core(CoreType.mtproto, [self.tag_b])}
        self.user_id = 0
        self.email = ""
        monkeypatch.setattr(node_manager, "_nodes", {})
        monkeypatch.setattr(node_manager, "_node_signatures", {})
        monkeypatch.setattr(node_manager, "_user_sync_locks", {})
        monkeypatch.setattr(node_manager, "_lock", RWLock(fast=True))
        self._real_create_node = app_node.create_node
        monkeypatch.setattr(app_node, "create_node", self._create_node)
        monkeypatch.setattr(core_manager, "get_cores", AsyncMock(side_effect=lambda ids: dict(self.cores)))
        monkeypatch.setattr(node_operation, "get_node_by_id", AsyncMock(side_effect=self._node_by_id))
        monkeypatch.setattr(manager_sync, "get_node_by_id", AsyncMock(side_effect=self._node_by_id))
        monkeypatch.setattr(manager_sync, "GetDB", GetTestDB)
        monkeypatch.setattr(node_sync, "GetDB", GetTestDB, raising=False)
        refresh = getattr(node_sync, "refresh_node_users", None)
        if refresh is not None:
            monkeypatch.setattr("app.node.bridge._refresh_handler", refresh)
        monkeypatch.setattr(node_operation, "update_node_status", AsyncMock())
        monkeypatch.setattr(node_operation, "bulk_update_node_status", AsyncMock())
        monkeypatch.setattr(node_operation, "publish_node_sync", AsyncMock())
        monkeypatch.setattr(node_operation.notification, "connect_node", AsyncMock())
        monkeypatch.setattr(node_operation.notification, "error_node", AsyncMock())

    async def __aenter__(self):
        self.user_id = await self._seed_user([self.tag_a, self.tag_b])
        self.email = str(self.user_id)
        return self

    async def __aexit__(self, *exc_info):
        for node_id in self.db_nodes:
            await node_manager.remove_node(node_id, remote_stop=False)
        await asyncio.sleep(0.05)

    def _create_node(self, **kwargs):
        real_class = type(self._real_create_node(**kwargs))
        kwargs.pop("connection")
        return _model_class(real_class)(self.remotes[int(kwargs["node_id"])], **kwargs)

    async def _node_by_id(self, _db, node_id, **_kwargs):
        return self.db_nodes.get(node_id)

    def node(self, core_id: int = 1, extras: tuple[int, ...] = ()) -> SimpleNamespace:
        node_id = next(_NODE_IDS)
        self.remotes[node_id] = Remote()
        db_node = SimpleNamespace(
            id=node_id,
            name=f"snapshot-{node_id}",
            address="127.0.0.1",
            port=1,
            api_port=1,
            server_ca=_ca_pem(),
            api_key=str(uuid4()),
            connection_type=NodeConnectionType.grpc,
            default_timeout=5,
            internal_timeout=5,
            proxy_url=None,
            usage_coefficient=1.0,
            status=NodeStatus.connected,
            core_config_id=core_id,
            additional_core_config_ids=list(extras) or None,
            keep_alive=0,
            xray_version=CORE_VERSION,
            node_version=NODE_VERSION,
            message=None,
        )
        self.db_nodes[node_id] = db_node
        return db_node

    def remote(self, db_node) -> Remote:
        return self.remotes[db_node.id]

    def user(self, inbounds: list[str] | None):
        return _serialize_user_for_node(self.user_id, SETTINGS, inbounds)

    async def boot(self, db_node, *, with_extras: bool = True):
        pg_node = await node_manager.update_node(db_node)
        primary = self.cores[db_node.core_config_id]
        users = [self.user([self.tag_a, self.tag_b])]
        await pg_node.start(config=primary.to_str(), backend_type=BACKEND_TYPES[primary.type], users=users)
        if with_extras:
            for core_id in db_node.additional_core_config_ids or []:
                extra = self.cores[core_id]
                await pg_node.add_backend(config=extra.to_str(), backend_type=BACKEND_TYPES[extra.type], users=users)
        assert self.remote(db_node).carries(self.email)
        return pg_node

    async def restart_panel(self, db_node) -> None:
        await node_manager.remove_node(db_node.id, remote_stop=False)
        await asyncio.sleep(0.05)

    async def _seed_user(self, tags: list[str]) -> int:
        async with TestSession() as session:
            group = Group(name=unique_name("snap_group"), inbounds=[ProxyInbound(tag=tag) for tag in tags])
            user = User(username=unique_name("snap_user"), proxy_settings=SETTINGS, status=UserStatus.active)
            session.add_all([group, user])
            await session.flush()
            await session.execute(users_groups_association.insert(), [{"user_id": user.id, "groups_id": group.id}])
            await session.commit()
            return user.id

    async def disable(self, *, bulk: bool = False, dispatch: bool = True) -> None:
        async with TestSession() as session:
            await session.execute(update(User).where(User.id == self.user_id).values(status=UserStatus.disabled))
            await session.commit()
        if not dispatch:
            return
        if bulk:
            await node_manager.update_users([self.user(None)])
        else:
            await node_manager.update_user(self.user(None))

    def wrap_update_node(self, before=None, after=None) -> None:
        original = node_manager.update_node
        calls = itertools.count()

        async def update_node(db_node):
            first = next(calls) == 0
            if first and before is not None:
                await before()
            pg_node = await original(db_node)
            if first and after is not None:
                await after()
            return pg_node

        self.monkeypatch.setattr(node_manager, "update_node", update_node)

    async def cleared(self, db_node) -> bool:
        return await eventually(lambda: not self.remote(db_node).carries(self.email))


def _operation() -> NodeOperation:
    return NodeOperation(operator_type=OperatorType.SYSTEM)


async def _bulk(nodes, *, force_start: bool, sync: bool = False) -> None:
    operation = _operation()
    connect = operation._connect_nodes_bulk_sync if sync else operation._connect_nodes_bulk_local
    async with TestSession() as db:
        await connect(db, nodes, force_start=force_start)


@pytest.mark.parametrize("sync", [False, True], ids=["local", "sync"])
async def test_force_first_node_acked_before_its_start(monkeypatch, sync):
    monkeypatch.setattr(node_operation, "CONNECT_CONCURRENCY", 1)
    async with Harness(monkeypatch) as h:
        first, second = h.node(1), h.node(1)
        await h.boot(first)
        await h.boot(second)

        async def delta_acked():
            await h.disable()
            assert await eventually(lambda: not h.remote(first).carries(h.email))

        h.wrap_update_node(after=delta_acked)
        await _bulk([first, second], force_start=True, sync=sync)

        assert await h.cleared(first), "the Start snapshot buried a delta the node had already acknowledged"


async def test_force_first_node_queued_then_cleared_by_stop(monkeypatch):
    monkeypatch.setattr(node_operation, "CONNECT_CONCURRENCY", 1)
    async with Harness(monkeypatch) as h:
        first, second = h.node(1), h.node(1)
        await h.boot(first)
        await h.boot(second)
        remote = h.remote(first)

        async def delta_left_queued():
            remote.stream_fails = True
            await h.disable()
            assert await eventually(lambda: remote.stream_attempts > 0)

        async def heal_stream():
            remote.stream_fails = False

        remote.on("Stop", heal_stream)
        h.wrap_update_node(after=delta_left_queued)
        await _bulk([first, second], force_start=True)

        assert await h.cleared(first), "Stop dropped the queued delta and Start pushed the older snapshot"


async def test_force_cross_core_acked_on_second(monkeypatch):
    monkeypatch.setattr(node_operation, "CONNECT_CONCURRENCY", 1)
    async with Harness(monkeypatch) as h:
        first, second = h.node(1), h.node(2)
        await h.boot(first)
        await h.boot(second)

        async def delta_acked_by_second():
            await h.disable()
            assert await eventually(lambda: not h.remote(second).carries(h.email))

        h.remote(first).on("Start", delta_acked_by_second)
        await _bulk([first, second], force_start=True)

        assert await h.cleared(second), "the second core's Start buried a delta it had already acknowledged"


async def test_force_same_core_snapshot_is_not_reused_for_a_later_node(monkeypatch):
    monkeypatch.setattr(node_operation, "CONNECT_CONCURRENCY", 1)
    async with Harness(monkeypatch) as h:
        first, second = h.node(1), h.node(1)
        await h.boot(first)
        await h.boot(second)

        async def delta_acked_by_second():
            await h.disable()
            assert await eventually(lambda: not h.remote(second).carries(h.email))

        h.remote(first).on("Start", delta_acked_by_second)
        await _bulk([first, second], force_start=True)

        assert await h.cleared(second), "a snapshot read before the second node stopped was reused for its Start"


async def test_startup_attach_node_not_registered_yet(monkeypatch):
    async with Harness(monkeypatch) as h:
        db_node = h.node(1)
        await h.boot(db_node)
        await h.restart_panel(db_node)

        h.wrap_update_node(before=h.disable)
        await _bulk([db_node], force_start=False)

        assert await h.cleared(db_node), "the attach pushed a snapshot older than a change made before registration"


async def test_startup_start_replays_a_delta_queued_after_the_read(monkeypatch):
    async with Harness(monkeypatch) as h:
        db_node = h.node(1)
        h.remote(db_node).on("Start", h.disable)

        await _bulk([db_node], force_start=False)

        assert h.remote(db_node).running
        assert await h.cleared(db_node), "a delta queued while the node was starting was never replayed"


async def test_force_start_replays_a_delta_queued_between_the_read_and_start(monkeypatch):
    async with Harness(monkeypatch) as h:
        db_node = h.node(1)
        await h.boot(db_node)
        h.remote(db_node).on("Start", h.disable)

        await _bulk([db_node], force_start=True)

        assert await h.cleared(db_node), "a delta queued after Stop was never replayed after Start"


async def test_force_start_keeps_a_bulk_push_made_while_the_core_was_stopped(monkeypatch):
    async with Harness(monkeypatch) as h:
        db_node = h.node(1)
        await h.boot(db_node)

        async def bulk_disable():
            await h.disable(bulk=True)
            await asyncio.sleep(0.2)

        h.remote(db_node).on("Start", bulk_disable)
        await _bulk([db_node], force_start=True)

        assert await h.cleared(db_node), "a bulk push sent to the stopped core was lost"


async def test_force_extra_core_add_backend_does_not_bury_a_delta(monkeypatch):
    async with Harness(monkeypatch) as h:
        db_node = h.node(1, extras=(2,))
        await h.boot(db_node)
        remote = h.remote(db_node)

        async def delta_during_add():
            await h.disable()
            await eventually(lambda: not remote.carries(h.email, service.BackendType.XRAY), timeout=0.5)

        remote.on("AddBackend", delta_during_add)
        await _bulk([db_node], force_start=True)

        assert await h.cleared(db_node), "the extra backend was added with a snapshot older than a delivered delta"


async def test_attach_keeps_the_users_of_every_backend(monkeypatch):
    async with Harness(monkeypatch) as h:
        db_node = h.node(1, extras=(2,))
        await h.boot(db_node)
        await h.restart_panel(db_node)

        await _bulk([db_node], force_start=False)
        await asyncio.sleep(0.3)

        remote = h.remote(db_node)
        assert remote.carries(h.email, service.BackendType.XRAY)
        assert remote.carries(h.email, service.BackendType.MTPROTO), (
            "attaching pushed only the primary core's users, which emptied the extra backend"
        )


async def test_attach_does_not_bury_a_delta_delivered_during_the_push(monkeypatch):
    async with Harness(monkeypatch) as h:
        db_node = h.node(1)
        await h.boot(db_node)
        await h.restart_panel(db_node)
        remote = h.remote(db_node)

        async def delta_during_push():
            await h.disable()
            await eventually(lambda: not remote.carries(h.email), timeout=0.5)

        remote.on("SyncUsers", delta_during_push)
        await _bulk([db_node], force_start=False)

        assert await h.cleared(db_node), "the attach snapshot overwrote a delta delivered while it was being pushed"


@pytest.mark.parametrize("scenario", ["acked_before_start", "queued_then_cleared_by_stop"])
async def test_single_node_restart_does_not_bury_a_newer_update(monkeypatch, scenario):
    async with Harness(monkeypatch) as h:
        db_node = h.node(1)
        await h.boot(db_node)
        remote = h.remote(db_node)

        async def acked():
            await h.disable()
            assert await eventually(lambda: not remote.carries(h.email))

        async def left_queued():
            remote.stream_fails = True
            await h.disable()
            assert await eventually(lambda: remote.stream_attempts > 0)

        async def heal_stream():
            remote.stream_fails = False

        remote.on("Stop", heal_stream)
        h.wrap_update_node(after=acked if scenario == "acked_before_start" else left_queued)
        async with TestSession() as db:
            await _operation().restart_node(db, db_node.id, SimpleNamespace(username="admin"))

        assert await h.cleared(db_node), f"{scenario}: restart_node buried a newer update"


async def test_single_node_connect_replays_a_delta_queued_before_start(monkeypatch):
    async with Harness(monkeypatch) as h:
        db_node = h.node(1)
        h.remote(db_node).on("Start", h.disable)

        async with TestSession() as db:
            await _operation().connect_single_node(db, db_node.id)

        assert h.remote(db_node).running
        assert await h.cleared(db_node), "a delta queued while the node was starting was never replayed"


async def test_sibling_connect_message_attaches_without_pushing_its_own_snapshot(monkeypatch):
    async with Harness(monkeypatch) as h:
        db_node = h.node(1)
        await h.boot(db_node)
        await h.restart_panel(db_node)
        pushed = []

        async def record_push():
            pushed.append("SyncUsers")

        h.remote(db_node).on("SyncUsers", record_push)
        h.wrap_update_node(before=h.disable)
        await manager_sync.handle_node_message({"action": "connect", "node_id": db_node.id, "origin": "sibling"})

        assert (await node_manager.get_node(db_node.id)) is not None
        assert pushed == [], "a sibling attach replaced the running core's users with its own snapshot"


async def test_node_worker_bulk_command_does_not_bury_an_acked_delta(monkeypatch):
    from app.node import worker as node_worker

    monkeypatch.setattr(node_operation, "CONNECT_CONCURRENCY", 1)
    async with Harness(monkeypatch) as h:
        first, second = h.node(1), h.node(1)
        await h.boot(first)
        await h.boot(second)

        async def delta_acked():
            await h.disable()
            assert await eventually(lambda: not h.remote(first).carries(h.email))

        h.wrap_update_node(after=delta_acked)
        monkeypatch.setattr(node_worker, "GetDB", GetTestDB)
        monkeypatch.setattr(node_worker, "get_nodes", AsyncMock(return_value=([first, second], 2)))
        monkeypatch.setattr(core_manager, "_reload_from_cache", AsyncMock())
        service_instance = node_worker.NodeWorkerService.__new__(node_worker.NodeWorkerService)
        service_instance._node_operator = _operation()

        await service_instance._connect_nodes_bulk({"node_ids": [first.id, second.id], "force_start": True})

        assert await h.cleared(first), "the node worker's bulk restart buried a delta the node had acknowledged"


async def test_remote_bulk_connect_only_publishes(monkeypatch):
    publish = AsyncMock()
    monkeypatch.setattr(node_operation.node_nats_client, "publish", publish)
    monkeypatch.setattr(NodeOperation, "_get_core_users_map", AsyncMock(side_effect=AssertionError("read")))
    nodes = [SimpleNamespace(id=5), SimpleNamespace(id=6)]

    await _operation()._connect_nodes_bulk_remote(None, nodes, force_start=True)

    publish.assert_awaited_once_with("connect_nodes_bulk", {"node_ids": [5, 6], "force_start": True})


async def test_reconcile_extra_cores_does_not_bury_a_delta(monkeypatch):
    async with Harness(monkeypatch) as h:
        db_node = h.node(1, extras=(2,))
        pg_node = await h.boot(db_node, with_extras=False)
        remote = h.remote(db_node)

        async def delta_during_add():
            await h.disable()
            await eventually(lambda: not remote.carries(h.email, service.BackendType.XRAY), timeout=0.5)

        remote.on("AddBackend", delta_during_add)
        async with TestSession() as db:
            problems = await NodeOperation._reconcile_extra_cores(db, pg_node, db_node)

        assert problems == ""
        assert remote.carries("0", service.BackendType.MTPROTO) is False
        assert service.BackendType.MTPROTO in remote.backends
        assert await h.cleared(db_node), "reconciling added the extra backend with a snapshot older than a delta"


@pytest.mark.parametrize("scenario", ["flush_drops_a_queued_delta", "push_overwrites_a_delivered_delta"])
async def test_manual_user_sync_does_not_bury_a_newer_update(monkeypatch, scenario):
    async with Harness(monkeypatch) as h:
        db_node = h.node(1)
        await h.boot(db_node)
        remote = h.remote(db_node)
        operation = _operation()
        monkeypatch.setattr(operation, "get_validated_node", AsyncMock(return_value=db_node))
        monkeypatch.setattr(node_operation, "NodeResponse", SimpleNamespace(model_validate=lambda node: node))

        if scenario == "flush_drops_a_queued_delta":
            original = node_manager.sync_full

            async def sync_full(node_id, users, **kwargs):
                remote.stream_fails = True
                await h.disable()
                assert await eventually(lambda: remote.stream_attempts > 0)
                remote.stream_fails = False
                return await original(node_id, users, **kwargs)

            monkeypatch.setattr(node_manager, "sync_full", sync_full)
            flush = True
        else:

            async def delta_during_push():
                await h.disable()
                await eventually(lambda: not remote.carries(h.email), timeout=0.5)

            remote.on("SyncUsers", delta_during_push)
            flush = False

        async with TestSession() as db:
            await operation.sync_node_users(db, db_node.id, flush_users=flush)

        assert await h.cleared(db_node), f"{scenario}: the manual sync buried a newer update"


async def test_bulk_push_to_a_broken_node_is_delivered_once_it_recovers(monkeypatch):
    async with Harness(monkeypatch) as h:
        db_node = h.node(1)
        pg_node = await h.boot(db_node)
        remote = h.remote(db_node)
        remote.stream_fails = True
        await pg_node.set_health(Health.BROKEN)

        await h.disable(bulk=True)
        await asyncio.sleep(0.3)
        assert remote.carries(h.email)

        remote.stream_fails = False
        await pg_node.set_health(Health.HEALTHY)

        assert await h.cleared(db_node), "a bulk change that failed while the node was broken was dropped for good"


async def test_reconnect_start_is_not_undone_by_a_stale_queued_payload(monkeypatch):
    async with Harness(monkeypatch) as h:
        db_node = h.node(1)
        await node_manager.update_node(db_node)
        await node_manager.update_user(h.user([h.tag_a, h.tag_b]))
        await h.disable(bulk=True)

        await _bulk([db_node], force_start=False)
        assert h.remote(db_node).running
        await node_manager.update_user(_serialize_user_for_node(h.user_id + 100_000, SETTINGS, None))
        await asyncio.sleep(1.5)

        assert not h.remote(db_node).carries(h.email), (
            "a payload queued before the user expired was replayed after the fresh Start and put the user back"
        )


async def test_an_older_commit_enqueued_last_does_not_win_on_the_node(monkeypatch):
    from app.node import sync as user_sync

    async with Harness(monkeypatch) as h:
        db_node = h.node(1)
        await h.boot(db_node)
        gate = asyncio.Event()
        slow: set[int] = set()
        real_blocked = user_sync._user_sync_blocked

        async def blocked_after_a_round_trip(db_user):
            if id(db_user) in slow:
                await gate.wait()
            return await real_blocked(db_user)

        monkeypatch.setattr(user_sync, "_user_sync_blocked", blocked_after_a_round_trip)
        async with TestSession() as first, TestSession() as second:
            edit = await first.get(User, h.user_id)
            edit.note = "renewed"
            await first.commit()
            slow.add(id(edit))
            first_push = asyncio.create_task(user_sync.sync_user(edit))
            await asyncio.sleep(0)

            disable = await second.get(User, h.user_id)
            disable.status = UserStatus.disabled
            await second.commit()
            await user_sync.sync_user(disable)
            assert await h.cleared(db_node)

            gate.set()
            await first_push
            await asyncio.sleep(1.0)

        assert not h.remote(db_node).carries(h.email), "the older commit's payload overwrote the newer state"


async def test_attach_after_a_timed_out_force_start_pushes_the_current_users(monkeypatch):
    async with Harness(monkeypatch) as h:
        db_node = h.node(1)
        await h.boot(db_node)
        remote = h.remote(db_node)
        await h.disable(dispatch=False)
        remote.lost = {"Stop", "Start"}

        await _bulk([db_node], force_start=True)

        assert remote.running
        assert await h.cleared(db_node), "the panel attached to the old core after a lost Start and pushed nothing"


async def test_health_check_attach_pushes_the_current_users(monkeypatch):
    from app.jobs import node_checker

    async with Harness(monkeypatch) as h:
        db_node = h.node(1)
        db_node.status = NodeStatus.error
        await h.boot(db_node)
        await h.restart_panel(db_node)
        pg_node = await node_manager.update_node(db_node)
        await h.disable(dispatch=False)
        monkeypatch.setattr(node_checker, "GetDB", GetTestDB)
        monkeypatch.setattr(node_checker, "after_healthy_node_check", AsyncMock())
        monkeypatch.setattr(NodeOperation, "_update_single_node_status", AsyncMock())

        await node_checker.process_node_health_check(db_node, pg_node)

        assert await pg_node.get_health() is Health.HEALTHY
        assert await h.cleared(db_node), "the health check attached to a running core and pushed nothing"


async def test_a_stale_bulk_copy_pushed_after_a_newer_single_change_does_not_win(monkeypatch):
    from app.node import sync as user_sync

    async with Harness(monkeypatch) as h:
        db_node = h.node(1)
        await h.boot(db_node)
        async with TestSession() as bulk_session, TestSession() as admin_session:
            stale = await bulk_session.get(User, h.user_id)
            await stale.awaitable_attrs.groups

            disable = await admin_session.get(User, h.user_id)
            disable.status = UserStatus.disabled
            await admin_session.commit()
            await user_sync.sync_user(disable)
            assert await h.cleared(db_node)

            assert stale.status is UserStatus.active
            await user_sync.sync_users([stale])
            await asyncio.sleep(1.0)

        assert not h.remote(db_node).carries(h.email), "a bulk push of a copy loaded earlier re-enabled the user"


async def test_a_node_disabled_right_after_it_was_registered_is_not_started(monkeypatch):
    async with Harness(monkeypatch) as h:
        db_node = h.node(1)
        db_node.status = NodeStatus.error
        operation = _operation()
        disconnects = []

        async def disabled_by_the_admin():
            db_node.status = NodeStatus.disabled
            disconnects.append(asyncio.create_task(operation._disconnect_single_node_local(db_node.id)))

        h.wrap_update_node(after=disabled_by_the_admin)
        async with TestSession() as db:
            await operation.connect_single_node(db, db_node.id)
        await asyncio.gather(*disconnects)

        assert await node_manager.get_node(db_node.id) is None
        assert await eventually(lambda: not h.remote(db_node).running), (
            "a node disabled during its reconnect was left running"
        )
