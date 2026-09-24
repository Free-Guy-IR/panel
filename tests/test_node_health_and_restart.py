import asyncio
import datetime
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from PasarGuardNodeBridge import Health
from PasarGuardNodeBridge.common.service_pb2 import StatType, User as ProtoUser
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.manager import core_manager as app_core_manager
from app.db.base import Base
from app.db.crud.node import create_node, get_node_by_id, update_node_status
from app.db.models import CoreConfig, Node, NodeConnectionType, NodeStatus
from app.fork.jobs import node_extras as extras_job
from app.fork.operation import node_extras as extras_operation
from app.jobs import node_checker, record_usages
from app.models.core import CoreType
from app.models.node import NodeCreate
from app.node import NodeManager
from app.operation import OperatorType, node as node_operation
from app.operation.node import NodeOperation


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


async def _no_bridge_memory():
    return None


class _NullDB:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def isolated_manager(monkeypatch):
    monkeypatch.setattr("app.node.ensure_bridge_memory", _no_bridge_memory)
    monkeypatch.setattr("app.node.get_bridge_memory", lambda: (None, None, None))
    manager = NodeManager()
    monkeypatch.setattr(node_operation, "node_manager", manager)
    return manager


@pytest_asyncio.fixture
async def black_hole():
    held = []

    async def swallow(reader, writer):
        held.append(writer)
        await asyncio.sleep(3600)

    server = await asyncio.start_server(swallow, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        for writer in held:
            writer.close()
        server.close()


async def test_one_unreachable_node_does_not_stretch_the_fleet_health_round(monkeypatch):
    hung = SimpleNamespace(id=57, name="unreachable")
    fine = SimpleNamespace(id=1, name="fine")
    checked = []

    async def fake_check(db_node, node):
        checked.append(db_node.id)
        if db_node.id == hung.id:
            await asyncio.sleep(60)

    monkeypatch.setattr(node_checker, "GetDB", _NullDB)
    monkeypatch.setattr(node_checker, "get_nodes", AsyncMock(return_value=([hung, fine], 2)))
    monkeypatch.setattr(node_checker.node_manager, "get_nodes", AsyncMock(return_value={57: object(), 1: object()}))
    monkeypatch.setattr(node_checker, "process_node_health_check", fake_check)
    monkeypatch.setattr(node_checker, "NODE_CHECK_TIMEOUT", 3)
    monkeypatch.setattr(node_checker, "runtime_settings", SimpleNamespace(role=SimpleNamespace(runs_node=True)))

    started = time.monotonic()
    await node_checker.node_health_check()
    first_round = time.monotonic() - started
    await asyncio.sleep(0.05)
    await node_checker.node_health_check()
    await asyncio.sleep(0.05)

    assert first_round < 0.5, f"one round took {first_round:.1f}s because it waited for the unreachable node"
    assert checked.count(hung.id) == 1, "a node whose check is still running was checked a second time"
    assert checked.count(fine.id) == 2
    await node_checker._stop_node_loops()


@pytest.mark.parametrize("probe_timeout, guard", [(0.5, 3), (10, 1)], ids=["probe_times_out", "guard_fires"])
async def test_a_hung_node_is_marked_broken_and_errored(monkeypatch, black_hole, isolated_manager, probe_timeout, guard):
    monkeypatch.setattr(node_checker, "NODE_CHECK_TIMEOUT", guard)
    monkeypatch.setattr(node_checker, "HEALTH_PROBE_TIMEOUT", probe_timeout, raising=False)
    statuses = []
    reconnects = []

    async def record_status(db, node_id, status, message="", **kwargs):
        statuses.append(status)

    async def record_reconnect(db, node_id, **kwargs):
        reconnects.append(node_id)

    monkeypatch.setattr(NodeOperation, "_update_single_node_status", staticmethod(record_status))
    monkeypatch.setattr(node_checker.node_operator, "connect_single_node", record_reconnect)
    monkeypatch.setattr(node_checker, "GetDB", _NullDB)

    db_node = Node(
        name="hung",
        address="127.0.0.1",
        port=black_hole,
        api_port=black_hole,
        server_ca=_ca_pem(),
        api_key=str(uuid4()),
        core_config_id=None,
        connection_type=NodeConnectionType.grpc,
        default_timeout=60,
        internal_timeout=60,
        status=NodeStatus.connected,
    )
    db_node.id = 57
    pg_node = await isolated_manager.update_node(db_node)
    await pg_node.connect("0.6.20", "26.3.27")

    started = time.monotonic()
    await node_checker._bounded_health_check(db_node, pg_node)
    elapsed = time.monotonic() - started

    assert elapsed < guard + 1
    assert await pg_node.get_health() is Health.BROKEN
    assert NodeStatus.error in statuses
    await isolated_manager.remove_node(db_node.id, remote_stop=False)


class _RestartableNode:
    def __init__(self, counters: dict[int, int]):
        self.counters = dict(counters)
        self.calls: list[str] = []

    async def get_lifecycle_state(self):
        return None

    async def get_extra(self):
        return {"usage_coefficient": 1.0}

    async def get_stats(self, stat_type, reset: bool = True, name: str = "", timeout=None):
        self.calls.append(f"get_stats:{StatType.Name(stat_type)}:{reset}")
        snapshot = dict(self.counters)
        if reset:
            self.counters = {uid: 0 for uid in snapshot}
        return SimpleNamespace(stats=[SimpleNamespace(name=str(uid), value=v) for uid, v in snapshot.items() if v])

    async def stop(self):
        self.calls.append("stop")
        self.counters = {uid: 0 for uid in self.counters}

    async def start(self, **kwargs):
        self.calls.append("start")
        self.counters = {uid: 0 for uid in self.counters}
        return SimpleNamespace(node_version="0.6.21", core_version="26.1.1", started=True)


async def test_a_forced_restart_drains_unbilled_counters_before_the_core_is_killed(monkeypatch):
    monkeypatch.setattr(record_usages, "current_usage_epochs", AsyncMock(return_value={7: 3}))
    monkeypatch.setattr(record_usages, "_retained_cohorts", [])
    node = _RestartableNode({7: 123_456})
    db_node = SimpleNamespace(id=1, name="n1", keep_alive=60)
    core = SimpleNamespace(to_str=lambda: "{}", type=None, exclude_inbound_tags=[])

    await NodeOperation._start_or_attach_node(node, db_node, core, [], 0, force_start=True)

    assert node.calls[0] == "get_stats:UsersStat:True", node.calls
    assert node.calls[1:] == ["stop", "start"]
    assert len(record_usages._retained_cohorts) == 1
    cohort = record_usages._retained_cohorts[0]
    assert cohort.api_params == {1: [{"uid": 7, "value": 123_456}]}
    assert cohort.epoch_at_poll == {7: 3}


async def test_shutdown_drains_and_bills_counters_before_stopping_cores(monkeypatch):
    node = _RestartableNode({7: 5_000})
    flushed = []

    async def flush():
        flushed.append(list(record_usages._retained_cohorts))
        record_usages._retained_cohorts.clear()

    monkeypatch.setattr(record_usages, "current_usage_epochs", AsyncMock(return_value={7: 0}))
    monkeypatch.setattr(record_usages, "_retained_cohorts", [])
    monkeypatch.setattr(record_usages, "_flush_retained_cohorts", flush)
    monkeypatch.setattr(node_checker.node_manager, "get_nodes", AsyncMock(return_value={4: node}))
    monkeypatch.setattr(node_checker, "runtime_settings", SimpleNamespace(role=SimpleNamespace(runs_node=True)))
    monkeypatch.setattr(node_checker, "is_multi_worker", lambda: False)

    await node_checker.shutdown_nodes()

    assert node.calls == ["get_stats:UsersStat:True", "stop"]
    assert len(flushed) == 1 and flushed[0][0].api_params == {4: [{"uid": 7, "value": 5_000}]}


CA = _ca_pem()


@pytest_asyncio.fixture
async def maker(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'reconnect.sqlite3'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def stored_node_id(maker):
    async with maker() as db:
        core = CoreConfig(name="reconnect-xray", type="xray", config={})
        db.add(core)
        await db.commit()
        await db.refresh(core)
        node = await create_node(
            db,
            NodeCreate(
                name="reconnect-node",
                address="127.0.0.1",
                port=64600,
                api_port=64601,
                connection_type="grpc",
                server_ca=CA,
                keep_alive=0,
                core_config_id=core.id,
                api_key="55555555-5555-5555-5555-555555555555",
            ),
        )
        await update_node_status(db, node, NodeStatus.error, message="Health check timeout")
        return node.id


@pytest.mark.parametrize("stop_status", [NodeStatus.limited, NodeStatus.disabled])
async def test_a_reconnect_that_read_the_node_before_it_was_stopped_does_not_restart_it(
    monkeypatch, maker, stored_node_id, isolated_manager, stop_status
):
    snapshot_taken = asyncio.Event()
    release = asyncio.Event()
    starts = []

    async def slow_users_map(db, core_ids, lazy=False):
        snapshot_taken.set()
        await release.wait()
        return {core_id: object() for core_id in core_ids}, {core_id: [] for core_id in core_ids}

    async def record_start(db_node, core, users, extra_cores=None, **kwargs):
        starts.append(db_node.id)

    monkeypatch.setattr(NodeOperation, "_get_core_users_map", staticmethod(slow_users_map))
    monkeypatch.setattr(NodeOperation, "connect_node", staticmethod(record_start))
    monkeypatch.setattr(node_operation, "GetDB", maker)
    operator = NodeOperation(operator_type=OperatorType.SYSTEM)

    async with maker() as health_db:
        reconnect = asyncio.create_task(operator._connect_single_node_local(health_db, stored_node_id))
        await asyncio.wait_for(snapshot_taken.wait(), timeout=5)
        async with maker() as limiter_db:
            limited = await get_node_by_id(limiter_db, stored_node_id, load_usage_logs=False)
            await update_node_status(limiter_db, limited, stop_status, message="stopped")
        await operator._disconnect_single_node_local(stored_node_id)
        release.set()
        await asyncio.wait_for(reconnect, timeout=5)

    assert await isolated_manager.get_node(stored_node_id) is None
    assert starts == []


def _replacement_node(address: str):
    return SimpleNamespace(
        id=424242,
        name="replaced-node",
        connection_type=NodeConnectionType.grpc,
        address=address,
        port=1,
        api_port=1,
        server_ca="test",
        api_key="00000000-0000-0000-0000-000000000000",
        default_timeout=1,
        internal_timeout=1,
        proxy_url=None,
        usage_coefficient=1,
    )


async def test_replacing_a_node_object_keeps_a_delta_queued_on_the_replacement(isolated_manager):
    manager = isolated_manager
    with patch("ssl.SSLContext.load_verify_locations"):
        old = await manager.update_node(_replacement_node("127.0.0.1"))
        issued = asyncio.Event()
        waiting = asyncio.Event()

        async def disable_during_replacement():
            try:
                waiting.set()
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await manager.update_user(ProtoUser(email="disabled-during-replacement", inbounds=[]))
                issued.set()
                raise

        await old.connect("0.6.4", "1.0", tasks=[disable_during_replacement])
        await waiting.wait()
        new = await manager.update_node(_replacement_node("127.0.0.2"))

    assert issued.is_set() and new is not old
    claimed = await new._user_sync_store.claim_users(new.node_id, "probe", 100, 30)
    try:
        assert [item.user.email for item in claimed] == ["disabled-during-replacement"]
    finally:
        await new._user_sync_store.requeue_users(new.node_id, claimed)

    await NodeOperation(operator_type=OperatorType.SYSTEM)._remove_node_local(424242)
    assert await new._user_sync_store.claim_users(new.node_id, "probe", 100, 30) == []


async def test_deferred_core_reads_run_in_a_fresh_session_on_the_callers_engine(monkeypatch):
    core = SimpleNamespace(inbounds=["in"], protocols=frozenset())
    monkeypatch.setattr(app_core_manager, "get_cores", AsyncMock(return_value={1: core}))
    seen = []

    async def read(*, db, inbound_tags, allowed_protocols):
        seen.append(db)
        return []

    monkeypatch.setattr(node_operation, "core_users", read)
    engine = create_async_engine("sqlite+aiosqlite://")
    caller = SimpleNamespace(bind=engine)

    _, loaders = await NodeOperation._get_core_users_map(caller, {1}, lazy=True)
    await loaders[1]()
    await engine.dispose()

    assert len(seen) == 1
    assert seen[0] is not caller
    assert seen[0].bind is engine


async def test_a_healthy_round_loads_no_users_for_an_extra_core_that_is_already_running(monkeypatch):
    from PasarGuardNodeBridge.common import service_pb2 as service

    loads = []

    async def users_map(db, core_ids, lazy=False):
        async def load(core_id):
            loads.append(core_id)
            return ["every wireguard user"]

        cores = {core_id: SimpleNamespace(type=CoreType.wg) for core_id in core_ids}
        users = {core_id: (lambda core_id=core_id: load(core_id)) for core_id in core_ids}
        if not lazy:
            users = {core_id: await load(core_id) for core_id in core_ids}
        return cores, users

    class RunningNode:
        def __init__(self):
            self.added = []
            self.listed = 0

        async def list_backends(self):
            self.listed += 1
            return SimpleNamespace(types=[service.BackendType.XRAY, service.BackendType.WIREGUARD])

        async def add_backend(self, **kwargs):
            self.added.append(kwargs)

    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(NodeOperation, "_get_core_users_map", staticmethod(users_map))
    monkeypatch.setattr(
        NodeOperation, "_resolve_core_without_users", staticmethod(AsyncMock(return_value=SimpleNamespace(type=CoreType.xray)))
    )
    monkeypatch.setattr(extras_job, "GetDB", _NullDB)
    monkeypatch.setattr(extras_job, "_attach_traffic_log", nothing)
    monkeypatch.setattr(extras_job, "_reconcile_content_filter", nothing)

    pg_node = RunningNode()
    monkeypatch.setattr(extras_operation, "node_manager", SimpleNamespace(get_node=AsyncMock(return_value=pg_node)))
    db_node = SimpleNamespace(id=68, name="node-68", core_config_id=1, additional_core_config_ids=[3])
    for _ in range(6):
        await extras_job.after_healthy_node_check(pg_node, db_node)

    assert pg_node.listed == 6
    assert pg_node.added == []
    assert loads == [], f"six healthy rounds loaded the running extra core's users {len(loads)} times"
