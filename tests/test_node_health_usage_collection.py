import asyncio
from uuid import uuid4

import pytest
from PasarGuardNodeBridge import Health, NodeAPIError
from PasarGuardNodeBridge.common import service_pb2 as service
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app import notification
from app.db import base
from app.db.models import Node, NodeStatus
from app.fork import node_health
from app.fork.node_health import USAGE_STALLED_PREFIX, UsageCollectionTracker
from app.jobs import node_checker, record_usages

FAILED_COLLECTION_CYCLES = 60
STATS_TIMEOUT = NodeAPIError(code=-1, detail="Timeout error: stats query did not answer")


class StatsDeadNode:
    def __init__(self, backend_error: Exception | None = None):
        self.health = Health.HEALTHY
        self.backend_error = backend_error
        self.backend_probes = 0
        self.stats_requests: list[tuple[int, bool]] = []

    def requires_hard_reset(self):
        return False

    async def get_health(self):
        return self.health

    async def set_health(self, health):
        self.health = health

    async def get_backend_stats(self, timeout=None):
        self.backend_probes += 1
        if self.backend_error is not None:
            raise self.backend_error
        return service.BackendStatsResponse()

    async def get_stats(self, stat_type, reset=True, name="", timeout=None):
        self.stats_requests.append((stat_type, reset))
        raise STATS_TIMEOUT

    async def get_extra(self):
        return {"usage_coefficient": 2.0}

    async def get_lifecycle_state(self):
        return None

    async def get_versions(self):
        return "0.9.1", "25.9.11"


class StoredNode:
    def __init__(self, sessions, node_id: int):
        self.sessions = sessions
        self.id = node_id

    async def load(self) -> Node:
        async with self.sessions() as db:
            return (await db.execute(select(Node).where(Node.id == self.id))).scalar_one()

    async def set_status(self, status: NodeStatus, message: str) -> None:
        async with self.sessions() as db:
            await db.execute(update(Node).where(Node.id == self.id).values(status=status, message=message))
            await db.commit()


@pytest.fixture
def tracker(monkeypatch: pytest.MonkeyPatch) -> UsageCollectionTracker:
    fresh = UsageCollectionTracker()
    monkeypatch.setattr(node_health, "usage_collection", fresh)
    return fresh


@pytest.fixture
async def stored_node(monkeypatch: pytest.MonkeyPatch, tracker):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine.sync_engine, "connect")
    def enforce_foreign_keys(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        assert (await conn.exec_driver_sql("PRAGMA foreign_keys")).scalar() == 1
        await conn.run_sync(base.Base.metadata.create_all)
    sessions = async_sessionmaker(autocommit=False, autoflush=False, expire_on_commit=False, bind=engine)
    monkeypatch.setattr(base, "SessionLocal", sessions)

    async with sessions() as db:
        row = Node(
            name="tr-2x",
            address="127.0.0.1",
            port=62050,
            api_port=62051,
            server_ca="ca",
            api_key=str(uuid4()),
            core_config_id=None,
            usage_coefficient=2.0,
            status=NodeStatus.connected,
        )
        db.add(row)
        await db.commit()
        node_id = row.id

    yield StoredNode(sessions, node_id)
    await engine.dispose()


@pytest.fixture
def side_effects(monkeypatch: pytest.MonkeyPatch):
    seen = {"errors": [], "recovered": [], "reconnects": [], "maintenance": []}

    async def error_node(node):
        seen["errors"].append(node)

    async def recovered_node(node):
        seen["recovered"].append(node)

    async def connect_single_node(db, node_id, **_):
        seen["reconnects"].append(node_id)

    async def after_healthy_node_check(node, db_node):
        seen["maintenance"].append(db_node.id)

    monkeypatch.setattr(notification, "error_node", error_node)
    monkeypatch.setattr(notification, "recovered_node", recovered_node)
    monkeypatch.setattr(node_checker.node_operator, "connect_single_node", connect_single_node)
    monkeypatch.setattr(node_checker, "after_healthy_node_check", after_healthy_node_check)
    return seen


async def run_check(stored: StoredNode, node) -> Node:
    await node_checker.process_node_health_check(await stored.load(), node)
    await asyncio.sleep(0)
    return await stored.load()


def fail_collection(node_id: int, times: int) -> None:
    for _ in range(times):
        node_health.record_usage_failure(node_id, STATS_TIMEOUT)


@pytest.mark.asyncio
async def test_the_health_check_passes_on_a_node_whose_usage_rpc_always_fails():
    node = StatsDeadNode()

    health, error_code, error_message = await node_checker.verify_node_backend_health(node, "tr-2x")
    collected = await record_usages.get_users_stats(node, 41)

    assert (health, error_code, error_message) == (Health.HEALTHY, None, None)
    assert node.backend_probes == 1
    assert collected == []
    assert node.stats_requests == [(service.StatType.UsersStat, True)]
    assert await node.get_health() is Health.HEALTHY


@pytest.mark.xfail(strict=True, reason="record_usages.get_users_stats does not report to app.fork.node_health yet")
@pytest.mark.asyncio
async def test_a_node_whose_usage_collection_always_fails_is_reported_instead_of_staying_connected(
    stored_node, side_effects
):
    node = StatsDeadNode()

    for _ in range(FAILED_COLLECTION_CYCLES):
        node_id, _, stats = await record_usages._collect_node_user_usage(node, stored_node.id)
        assert (node_id, stats) == (stored_node.id, [])

    stored = await run_check(stored_node, node)

    assert stored.status == NodeStatus.error
    assert stored.message.startswith(USAGE_STALLED_PREFIX)
    assert [sent.id for sent in side_effects["errors"]] == [stored_node.id]
    assert side_effects["reconnects"] == []


def test_only_an_unbroken_run_of_failures_reaches_the_threshold(tracker):
    fail_collection(7, tracker.threshold - 1)
    assert not tracker.is_stalled(7)

    node_health.record_usage_success(7)
    assert tracker.failures(7) == 0
    assert tracker.observed(7)

    fail_collection(7, tracker.threshold - 1)
    assert not tracker.is_stalled(7)
    fail_collection(7, 1)
    assert tracker.is_stalled(7)


def test_a_collection_without_a_node_id_is_not_tracked(tracker):
    node_health.record_usage_failure(None, STATS_TIMEOUT)
    node_health.record_usage_success(None)
    assert not tracker._streaks


def test_the_stalled_message_names_the_streak_and_the_last_error(tracker):
    fail_collection(7, tracker.threshold)
    node_health.record_usage_failure(7, TimeoutError())

    message = tracker.stalled_message(7)

    assert message.startswith(USAGE_STALLED_PREFIX)
    assert f"{tracker.threshold + 1} attempts in a row" in message
    assert message.endswith("Last error: TimeoutError")
    assert len(message) <= 1024


def test_a_long_error_is_cut_so_the_status_message_fits_its_column(tracker):
    node_health.record_usage_failure(7, NodeAPIError(code=500, detail="x" * 5000))
    fail_collection(7, tracker.threshold)
    node_health.record_usage_failure(7, NodeAPIError(code=500, detail="y" * 5000))

    assert "NodeAPIError(code=500" in tracker.stalled_message(7)
    assert len(tracker.stalled_message(7)) <= 1024


@pytest.mark.asyncio
async def test_a_stalled_node_is_marked_error_once_without_a_reconnect(stored_node, side_effects, tracker):
    fail_collection(stored_node.id, tracker.threshold)
    node = StatsDeadNode()

    first = await run_check(stored_node, node)
    fail_collection(stored_node.id, 5)
    second = await run_check(stored_node, node)

    assert first.status == NodeStatus.error
    assert first.message.startswith(f"{USAGE_STALLED_PREFIX}: {tracker.threshold} attempts in a row")
    assert second.status == NodeStatus.error
    assert second.message == first.message
    assert second.last_status_change == first.last_status_change
    assert [sent.id for sent in side_effects["errors"]] == [stored_node.id]
    assert side_effects["reconnects"] == []
    assert side_effects["recovered"] == []
    assert side_effects["maintenance"] == [stored_node.id, stored_node.id]


@pytest.mark.asyncio
async def test_a_stalled_node_is_recovered_once_collection_succeeds(stored_node, side_effects, tracker):
    fail_collection(stored_node.id, tracker.threshold)
    node = StatsDeadNode()
    await run_check(stored_node, node)

    node_health.record_usage_success(stored_node.id)
    recovered = await run_check(stored_node, node)

    assert recovered.status == NodeStatus.connected
    assert recovered.message == ""
    assert [sent.id for sent in side_effects["recovered"]] == [stored_node.id]
    assert side_effects["reconnects"] == []


@pytest.mark.asyncio
async def test_failures_below_the_threshold_leave_the_node_alone(stored_node, side_effects, tracker):
    fail_collection(stored_node.id, tracker.threshold - 1)

    stored = await run_check(stored_node, StatsDeadNode())

    assert stored.status == NodeStatus.connected
    assert stored.message is None
    assert side_effects["errors"] == []
    assert side_effects["maintenance"] == [stored_node.id]


@pytest.mark.asyncio
async def test_a_flag_raised_by_the_collecting_process_is_not_cleared_by_one_without_evidence(
    stored_node, side_effects, tracker
):
    await stored_node.set_status(NodeStatus.error, f"{USAGE_STALLED_PREFIX}: 10 attempts in a row returned no stats")

    stored = await run_check(stored_node, StatsDeadNode())

    assert stored.status == NodeStatus.error
    assert stored.message.startswith(USAGE_STALLED_PREFIX)
    assert side_effects["recovered"] == []
    assert side_effects["maintenance"] == [stored_node.id]


@pytest.mark.asyncio
async def test_an_error_with_another_cause_still_recovers_as_before(stored_node, side_effects, tracker):
    await stored_node.set_status(NodeStatus.error, "Health check timeout")

    stored = await run_check(stored_node, StatsDeadNode())

    assert stored.status == NodeStatus.connected
    assert [sent.id for sent in side_effects["recovered"]] == [stored_node.id]


@pytest.mark.asyncio
async def test_a_broken_node_keeps_the_existing_broken_handling(stored_node, side_effects, tracker):
    fail_collection(stored_node.id, tracker.threshold)

    stored = await run_check(stored_node, StatsDeadNode(backend_error=NodeAPIError(code=500, detail="xray api down")))

    assert stored.status == NodeStatus.error
    assert stored.message == "xray api down"
    assert side_effects["reconnects"] == [stored_node.id]
    assert side_effects["maintenance"] == []
