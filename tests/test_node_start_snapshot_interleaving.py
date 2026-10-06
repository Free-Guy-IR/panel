import asyncio
import contextlib
import json
import random
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import certifi
import pytest
from aiorwlock import RWLock
from PasarGuardNodeBridge import InMemoryNodeLifecycleCoordinator, InMemoryUserSyncStore
from PasarGuardNodeBridge.common import service_pb2 as service
from PasarGuardNodeBridge.grpclib import Node as GrpcNode
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.manager import core_manager
from app.db.models import Group, ProxyInbound, User, UserStatus, users_groups_association
from app.models.core import CoreType
from app.models.proxy import ProxyTable
from app.nats.kv_cas import MemoryCasKv
from app.node import bridge as bridge_module, node_manager, sync as node_sync
from app.node.nats_memory import NatsUserSyncStore
from app.node.user import _serialize_user_for_node
from app.operation import OperatorType, node as node_operation
from app.operation.node import NodeOperation
from tests.api import DATABASE_URL, IS_SQLITE
from tests.api.helpers import unique_name

ROUNDS = 8
NODE_VERSION = "0.9.0"
CORE_VERSION = "26.3.27"


class Remote:
    def __init__(self, name: str, tags: set[str], rng: random.Random, log: list):
        self.name = name
        self.tags = tags
        self.rng = rng
        self.log = log
        self.running = False
        self.users: dict[str, tuple[str, tuple[str, ...]]] = {}

    def _state(self, user) -> tuple[str, tuple[str, ...]] | None:
        inbounds = tuple(sorted(set(user.inbounds) & self.tags))
        return (user.proxies.vless.id, inbounds) if inbounds else None

    def info(self):
        return service.BaseInfoResponse(started=self.running, node_version=NODE_VERSION, core_version=CORE_VERSION)

    async def rpc(self, name: str, request):
        await asyncio.sleep(self.rng.uniform(0, 0.01))
        if name == "Start":
            self.users = {user.email: state for user in request.users if (state := self._state(user)) is not None}
            self.running = True
            self.log.append(("start", self.name, dict(self.users)))
            return self.info()
        if name == "Stop":
            self.running = False
            self.users = {}
            return service.Empty()
        if name == "GetBaseInfo":
            return self.info()
        raise AssertionError(f"unexpected RPC {name}")

    async def delta(self, users) -> list:
        await asyncio.sleep(self.rng.uniform(0, 0.004))
        if not self.running:
            return list(users)
        for user in users:
            state = self._state(user)
            if state is None:
                self.users.pop(user.email, None)
            else:
                self.users[user.email] = state
            self.log.append(("delta", self.name, user.email, state))
        return []


class ModelTransport(GrpcNode):
    async def _handle_grpc_request(self, method, request, timeout=None):
        return await self.remote.rpc(method.name.rsplit("/", 1)[-1], request)

    async def sync_users_chunked(self, users, chunk_size=100, flush_pending=False, timeout=None):
        async with self._node_lock:
            return await self.remote.delta(users)

    async def _sync_batch_users(self, users):
        return await self.remote.delta(users)


class ModelNode(bridge_module._QueuedBatchSync, ModelTransport):
    @contextlib.asynccontextmanager
    async def full_sync_fence(self, lease_seconds=None):
        async with super().full_sync_fence(lease_seconds) as held:
            self.remote.log.append(("fence", self.remote.name))
            yield held


def _core(tags: list[str]):
    return SimpleNamespace(
        type=CoreType.xray,
        inbounds=list(tags),
        protocols=frozenset(),
        exclude_inbound_tags=[],
        to_str=lambda: json.dumps({"tags": list(tags)}),
    )


def _settings(vless_id: str) -> dict:
    settings = ProxyTable().dict(no_obj=True)
    settings["vless"]["id"] = vless_id
    return settings


class Round:
    def __init__(self, monkeypatch, rng: random.Random, store, nodes_per_core: int, writer, reader):
        self.rng = rng
        self.store = store
        self.writer = writer
        self.reader = reader
        self.log: list = []
        self.tags = {name: unique_name(f"snap_{name}") for name in "abc"}
        self.cores = {index: _core([self.tags[name]]) for index, name in enumerate("abc", start=1)}
        self.group_tags = {"ab": ["a", "b"], "bc": ["b", "c"], "c": ["c"]}
        self.memberships = [["ab"], ["bc"], ["ab", "c"], ["c"]]
        self.truth: dict[str, tuple[bool, str]] = {}
        self.user_tags: dict[str, set[str]] = {}
        self.db_nodes: dict[int, SimpleNamespace] = {}
        self.nodes: dict[int, ModelNode] = {}
        self.lifecycle = InMemoryNodeLifecycleCoordinator()
        for core_id in self.cores:
            for _ in range(nodes_per_core):
                self._add_node(core_id)
        monkeypatch.setattr(core_manager, "get_cores", AsyncMock(side_effect=lambda ids: dict(self.cores)))
        monkeypatch.setattr(node_manager, "_nodes", dict(self.nodes))
        monkeypatch.setattr(node_manager, "_user_sync_locks", {})
        monkeypatch.setattr(node_manager, "_lock", RWLock(fast=True))
        monkeypatch.setattr(node_manager, "update_node", self._update_node)
        order = list(self.nodes)
        rng.shuffle(order)
        self.late = set(order[: rng.randint(1, len(order) - 1)])
        self.entry_delay = {node_id: rng.uniform(0, 0.01) for node_id in order}
        self.first_read = asyncio.Event()
        self.settled = asyncio.Event()

    def _add_node(self, core_id: int) -> None:
        node_id = random.randrange(10**8, 10**9)
        name = f"n{len(self.nodes)}c{core_id}"
        node = ModelNode(
            address="localhost",
            port=1,
            api_port=1,
            server_ca=certifi.contents(),
            api_key=str(uuid4()),
            name=name,
            node_id=uuid4().hex,
            user_sync_store=self.store,
            lifecycle_coordinator=self.lifecycle,
        )
        node._sync_poll_interval = 0.005
        node._worker_idle_timeout = 0.05
        node.remote = Remote(name, set(self.cores[core_id].inbounds), random.Random(self.rng.random()), self.log)
        self.nodes[node_id] = node
        self.db_nodes[node_id] = SimpleNamespace(
            id=node_id, name=name, status=node_operation.NodeStatus.connected, core_config_id=core_id, keep_alive=0
        )

    async def _update_node(self, db_node):
        if db_node.id in self.late:
            await self.settled.wait()
            await self._caught_up(db_node.id)
        await asyncio.sleep(self.entry_delay[db_node.id])
        return self.nodes[db_node.id]

    async def _caught_up(self, node_id: int) -> None:
        node = self.nodes[node_id]
        deadline = asyncio.get_running_loop().time() + 3
        while asyncio.get_running_loop().time() < deadline:
            if node.remote.users == self.expected(node_id) and not await self.store.has_pending(node.node_id):
                return
            await asyncio.sleep(0.005)

    async def seed(self) -> None:
        async with self.writer() as session:
            inbounds = {name: ProxyInbound(tag=tag) for name, tag in self.tags.items()}
            groups = {
                key: Group(name=unique_name(f"snap_{key}"), inbounds=[inbounds[name] for name in names])
                for key, names in self.group_tags.items()
            }
            session.add_all(groups.values())
            users = []
            for keys in self.memberships:
                vless_id = str(uuid4())
                user = User(username=unique_name("snap"), proxy_settings=_settings(vless_id), status=UserStatus.active)
                users.append((user, keys, vless_id))
            session.add_all([user for user, _, _ in users])
            await session.flush()
            await session.execute(
                users_groups_association.insert(),
                [{"user_id": user.id, "groups_id": groups[key].id} for user, keys, _ in users for key in keys],
            )
            await session.commit()
        for user, keys, vless_id in users:
            email = str(user.id)
            self.truth[email] = (True, vless_id)
            self.user_tags[email] = {self.tags[name] for key in keys for name in self.group_tags[key]}

    def expected(self, node_id: int) -> dict[str, tuple[str, tuple[str, ...]]]:
        tags = set(self.cores[self.db_nodes[node_id].core_config_id].inbounds)
        expected = {}
        for email, (active, vless_id) in self.truth.items():
            inbounds = tuple(sorted(self.user_tags[email] & tags))
            if active and inbounds:
                expected[email] = (vless_id, inbounds)
        return expected

    def payload(self, email: str):
        active, vless_id = self.truth[email]
        inbounds = sorted(self.user_tags[email]) if active else None
        return _serialize_user_for_node(int(email), _settings(vless_id), inbounds)

    async def boot(self) -> None:
        for node_id, node in self.nodes.items():
            core = self.cores[self.db_nodes[node_id].core_config_id]
            users = [self.payload(email) for email in self.truth]
            users = [user for user in users if set(user.inbounds) & set(core.inbounds)]
            await node.start(config=core.to_str(), backend_type=service.BackendType.XRAY, users=users)
        assert all(node.remote.users == self.expected(node_id) for node_id, node in self.nodes.items())
        self.log.clear()

    def plan(self, count: int) -> list[tuple[float, bool]]:
        return [(self.rng.uniform(0, 0.01), self.rng.random() < 0.3) for _ in range(count)]

    async def writers(self) -> None:
        first = {email: self.plan(self.rng.randint(1, 3)) for email in self.truth}
        later = {email: self.plan(1) for email in self.truth if self.rng.random() < 0.5}
        await self.first_read.wait()
        await asyncio.gather(*(self.write(email, changes) for email, changes in first.items()))
        self.settled.set()
        await asyncio.gather(*(self.write(email, changes) for email, changes in later.items()))

    async def write(self, email: str, changes: list[tuple[float, bool]]) -> None:
        for delay, flip in changes:
            await asyncio.sleep(delay)
            active, vless_id = self.truth[email]
            if flip:
                active = not active
            else:
                vless_id = str(uuid4())
            async with self.writer() as session:
                await session.execute(
                    update(User)
                    .where(User.id == int(email))
                    .values(
                        status=UserStatus.active if active else UserStatus.disabled,
                        proxy_settings=_settings(vless_id),
                    )
                )
                await session.commit()
            self.truth[email] = (active, vless_id)
            self.log.append(("commit", email, (active, vless_id)))
            await node_manager.update_user(self.payload(email))

    async def restart(self) -> None:
        operation = NodeOperation(operator_type=OperatorType.SYSTEM)
        async with self.reader() as db:
            await db.execute(select(User.id).limit(1))
            await operation._connect_nodes_bulk_local(db, list(self.db_nodes.values()), force_start=True)

    def converged(self) -> bool:
        return all(node.remote.users == self.expected(node_id) for node_id, node in self.nodes.items())

    def explain(self, seed: int) -> str:
        lines = [f"seed {seed}: a node ended with an older user state than the database"]
        for node_id, node in self.nodes.items():
            expected, actual = self.expected(node_id), node.remote.users
            for email in sorted(set(expected) | set(actual)):
                if expected.get(email) == actual.get(email):
                    continue
                lines.append(
                    f"node {node.remote.name} user {email}: holds {actual.get(email)}, database {expected.get(email)}"
                )
                for index, entry in enumerate(self.log):
                    kind = entry[0]
                    if kind == "commit" and entry[1] == email:
                        lines.append(f"  #{index} commit {entry[2]}")
                    elif kind == "read":
                        lines.append(f"  #{index} snapshot read")
                    elif kind == "fence" and entry[1] == node.remote.name:
                        lines.append(f"  #{index} fence began on {entry[1]}")
                    elif kind == "delta" and entry[1] == node.remote.name and entry[2] == email:
                        lines.append(f"  #{index} delta applied on {entry[1]}: {entry[3]}")
                    elif kind == "start" and entry[1] == node.remote.name:
                        lines.append(f"  #{index} Start on {entry[1]} carried {entry[2].get(email)}")
        return "\n".join(lines)

    async def close(self) -> None:
        for node in self.nodes.values():
            await node.disconnect()
            node.channel.close()
            await node._json_client.close()


def _stores():
    return {"in-memory": lambda: InMemoryUserSyncStore(), "nats-kv": lambda: NatsUserSyncStore(MemoryCasKv())}


def _engine():
    return create_async_engine(
        DATABASE_URL, poolclass=NullPool, connect_args={"check_same_thread": False} if IS_SQLITE else {}
    )


@pytest.mark.parametrize("store_kind", ["in-memory", "nats-kv"])
@pytest.mark.parametrize("nodes_per_core", [1, 2], ids=["one-node-per-core", "two-nodes-per-core"])
async def test_bulk_restart_never_starts_a_node_with_users_older_than_it_already_acknowledged(
    monkeypatch, store_kind, nodes_per_core
):
    monkeypatch.setattr(node_operation, "bulk_update_node_status", AsyncMock())
    monkeypatch.setattr(node_operation.notification, "connect_node", AsyncMock())
    monkeypatch.setattr(node_operation.notification, "error_node", AsyncMock())
    log: list = []
    first_read = asyncio.Event()

    def record_snapshot_read(_conn, _cursor, statement, *_args):
        if "inbounds_groups_association" in statement and "users.id IN" not in statement:
            log.append(("read",))
            first_read.set()

    writer_engine, reader_engine = _engine(), _engine()
    writer = async_sessionmaker(bind=writer_engine, expire_on_commit=False)
    reader = async_sessionmaker(bind=reader_engine, expire_on_commit=False)
    monkeypatch.setattr(node_sync, "GetDB", reader)
    monkeypatch.setattr(node_sync, "_current_state", node_sync.CurrentStateReader())
    monkeypatch.setattr(bridge_module, "_refresh_handler", node_sync.refresh_node_users)
    event.listen(reader_engine.sync_engine, "before_cursor_execute", record_snapshot_read)
    try:
        for seed in range(ROUNDS):
            rng = random.Random(f"{store_kind}-{nodes_per_core}-{seed}")
            store = _stores()[store_kind]()
            current = Round(monkeypatch, rng, store, nodes_per_core, writer, reader)
            log, first_read = current.log, current.first_read
            try:
                await current.seed()
                await current.boot()
                async with asyncio.timeout(20):
                    await asyncio.gather(current.restart(), current.writers())
                deadline = asyncio.get_running_loop().time() + 3
                while not current.converged() and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(0.02)
                assert current.converged(), current.explain(seed)
            finally:
                await current.close()
                if isinstance(store, NatsUserSyncStore):
                    await store.close()
    finally:
        event.remove(reader_engine.sync_engine, "before_cursor_execute", record_snapshot_read)
        await writer_engine.dispose()
        await reader_engine.dispose()
