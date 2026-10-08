import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime as dt, timedelta as td
from types import SimpleNamespace
from uuid import uuid4

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from PasarGuardNodeBridge import Health, NodeAPIError, PasarGuardNode
from PasarGuardNodeBridge.storage import InMemoryNodeLifecycleCoordinator, InMemoryUserSyncStore
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Group, NodeStatus, ProxyInbound, User, UserStatus, users_groups_association
from app.models.proxy import ProxyTable
from tests.api import DATABASE_URL, IS_SQLITE, TestSession
from tests.api.helpers import unique_name

INFO = SimpleNamespace(started=True, node_version="0.6.4", core_version="26.3.27")


def _ca_pem() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "a1-audit")])
    now = dt.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - td(days=1))
        .not_valid_after(now + td(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode()


CA_PEM = _ca_pem()


class RecordingNode(PasarGuardNode):
    def __init__(self, node_id: int):
        super().__init__(
            server_ca=CA_PEM,
            api_key=str(uuid4()),
            service_url="https://127.0.0.1:9/",
            name=f"n{node_id}",
            logger=logging.getLogger(f"a1-node-{node_id}"),
            node_id=str(node_id),
            user_sync_store=InMemoryUserSyncStore(),
            lifecycle_coordinator=InMemoryNodeLifecycleCoordinator(),
            sync_poll_interval=0.01,
        )
        self._worker_idle_timeout = 0.3
        self.live: dict[str, list[str]] = {}
        self.reachable = True
        self.deliveries: list[tuple[str, str, list[str]]] = []

    def _apply(self, kind, users):
        for user in users:
            self.deliveries.append((kind, user.email, sorted(user.inbounds)))
            if user.inbounds:
                self.live[user.email] = sorted(user.inbounds)
            else:
                self.live.pop(user.email, None)

    def _can_deliver(self) -> bool:
        return self.reachable and bool(self._node_version)

    async def start(self, config, backend_type, users, keep_alive=0, exclude_inbounds=None, timeout=None):
        if not self.reachable:
            raise NodeAPIError(-1, "Request timed out")
        self.live = {user.email: sorted(user.inbounds) for user in users}
        self.deliveries.append(("start", "*", sorted(self.live)))
        await self.connect(INFO.node_version, INFO.core_version)
        return INFO

    async def stop(self, timeout=None):
        await self.disconnect()

    async def info(self, timeout=None):
        if not self.reachable:
            raise NodeAPIError(-1, "Request timed out")
        return INFO

    async def sync_users(self, users, flush_pending=False, timeout=None):
        if flush_pending:
            await self.flush_pending_users()
        if not self._can_deliver():
            raise NodeAPIError(-1, "Request timed out")
        self.live = {user.email: sorted(user.inbounds) for user in users}

    async def sync_users_chunked(self, users, chunk_size=100, flush_pending=False, timeout=None):
        if not self._can_deliver():
            return list(users)
        self._apply("chunked", users)
        return []

    async def _sync_batch_users(self, users):
        if not self._can_deliver():
            return list(users)
        self._apply("queued", users)
        return []

    async def get_backend_stats(self, timeout=None):
        if not self.reachable:
            raise NodeAPIError(-1, "Request timed out")
        return SimpleNamespace()

    async def get_system_stats(self, timeout=None):
        return None

    async def get_stats(self, stat_type, reset=True, name="", timeout=None):
        return None

    async def get_outbounds_latency(self, name="", timeout=None):
        return None

    async def get_user_online_stats(self, email, timeout=None):
        return None

    async def get_user_online_ip_list(self, email, timeout=None):
        return None

    async def list_routing_rules(self, timeout=None):
        return None

    async def get_balancer_info(self, tag, timeout=None):
        return None

    async def test_route(self, **kwargs):
        return None

    async def add_routing_rule(self, rule, should_reset=False, timeout=None):
        return None

    async def remove_routing_rule(self, rule_tag, timeout=None):
        return None

    async def override_balancer_target(self, balancer_tag, target, timeout=None):
        return None

    async def add_backend(self, config, backend_type, users, exclude_inbounds=None, timeout=None):
        return None

    async def remove_backend(self, backend_type, timeout=None):
        return None

    async def list_backends(self, timeout=None):
        return None

    async def _check_node_health(self):
        return None

    @asynccontextmanager
    async def stream_logs(self, max_queue_size=1000):
        yield asyncio.Queue()


def core_for(tag: str):
    return SimpleNamespace(type=None, inbounds=[tag], protocols=frozenset(), exclude_inbound_tags=[], to_str=lambda: "{}")


def db_node_for(node_id: int, status=NodeStatus.connected):
    return SimpleNamespace(
        id=node_id,
        name=f"n{node_id}",
        status=status,
        message="",
        core_config_id=1,
        additional_core_config_ids=[],
        keep_alive=60,
    )


async def seed_group(tag: str, *, sessions=TestSession) -> int:
    async with sessions() as session:
        group = Group(name=unique_name("a1_group"), inbounds=[ProxyInbound(tag=tag)])
        session.add(group)
        await session.commit()
        return group.id


async def seed_user(group_id: int, *, sessions=TestSession, **fields) -> int:
    async with sessions() as session:
        user = User(
            username=unique_name("a1_user"), proxy_settings=ProxyTable().dict(no_obj=True), status=UserStatus.active
        )
        for name, value in fields.items():
            setattr(user, name, value)
        session.add(user)
        await session.flush()
        await session.execute(users_groups_association.insert(), [{"user_id": user.id, "groups_id": group_id}])
        await session.commit()
        return user.id


class TestGetDB:
    sessions = TestSession

    def __init__(self):
        self.db = self.sessions()

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, exc_type, exc_value, traceback):
        if exc_type is not None:
            await self.db.rollback()
        await self.db.close()


def get_db_for(sessions) -> type[TestGetDB]:
    return type("SessionsGetDB", (TestGetDB,), {"sessions": sessions})


@asynccontextmanager
async def concurrent_sessions():
    if not IS_SQLITE:
        yield TestSession
        return
    if ":memory:" in DATABASE_URL:
        raise RuntimeError(f"concurrent sessions need a connection each, which {DATABASE_URL} cannot give")
    engine = create_async_engine(DATABASE_URL, connect_args={"check_same_thread": False, "uri": True})

    @event.listens_for(engine.sync_engine, "connect")
    def _per_connection_pragmas(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    try:
        yield async_sessionmaker(autocommit=False, autoflush=False, expire_on_commit=False, bind=engine)
    finally:
        await engine.dispose()


async def settle(predicate=None, timeout: float = 5.0):
    if predicate is None:
        await asyncio.sleep(0.5)
        return True
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        await asyncio.sleep(0.05)
        if predicate is not None and predicate():
            return True
    return predicate() if predicate is not None else True


def isolate_node_manager(monkeypatch, node_manager, nodes: dict) -> None:
    from aiorwlock import RWLock

    monkeypatch.setattr(node_manager, "_nodes", nodes)
    monkeypatch.setattr(node_manager, "_lock", RWLock(fast=True))
    monkeypatch.setattr(node_manager, "_user_sync_locks", {})


def healthy(node: RecordingNode) -> bool:
    return node._health is Health.HEALTHY
