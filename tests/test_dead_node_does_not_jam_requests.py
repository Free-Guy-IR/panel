import asyncio
import copy
import importlib
import os
import time
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from PasarGuardNodeBridge import Health
from PasarGuardNodeBridge.common import service_pb2 as service
from sqlalchemy import event, inspect, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.node as app_node
from app import notification
from app.db import base
from app.db.models import Node, NodeConnectionType, NodeStatus
from app.fork.operation import node_extras
from app.jobs import node_checker
from app.node import node_manager
from app.operation import node as node_operation
from app.operation.node import NodeOperation
from tests.api import DATABASE_URL, IS_SQLITE, app
from tests.api.helpers import unique_name
from tests.api.sample_data import XRAY_CONFIG
from tests.test_node_connect_stale_snapshot import CORE_VERSION, Remote, _ca_pem, _model_class

PROD_DEFAULT_TIMEOUT = 60
PROD_INTERNAL_TIMEOUT = 15
PROD_NODE_CHECK_TIMEOUT = 60
SCALE = 3
DEAD_DEFAULT_TIMEOUT = PROD_DEFAULT_TIMEOUT // SCALE
DEAD_INTERNAL_TIMEOUT = PROD_INTERNAL_TIMEOUT // SCALE
NODE_CHECK_TIMEOUT = PROD_NODE_CHECK_TIMEOUT // SCALE
ANSWER_WITHIN = 8.0
GIVE_UP_AFTER = 12 * DEAD_DEFAULT_TIMEOUT
HEALTH_CHECK_PROBE = "Health check timeout"
DEAD_RESTART_BUDGET = 5 + 4 * DEAD_DEFAULT_TIMEOUT + DEAD_INTERNAL_TIMEOUT
NOTIFICATIONS = (
    "create_core",
    "modify_core",
    "remove_core",
    "create_node",
    "modify_node",
    "remove_node",
    "connect_node",
    "error_node",
    "recovered_node",
    "admin_login",
)

if IS_SQLITE:
    own_connections = create_async_engine(
        DATABASE_URL, connect_args={"check_same_thread": False, "uri": True}, poolclass=NullPool
    )

    @event.listens_for(own_connections.sync_engine, "connect")
    def _enforce_sqlite_foreign_keys(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

else:
    own_connections = create_async_engine(DATABASE_URL, poolclass=NullPool)

Session = async_sessionmaker(autocommit=False, autoflush=False, expire_on_commit=False, bind=own_connections)


def background_connects():
    try:
        return importlib.import_module("app.fork.operation.node_background_connects")
    except ModuleNotFoundError:
        return None


def load() -> str:
    return f"load {os.getloadavg()[0]:.0f}"


def report(measured: str) -> str:
    print(f"MEASURED {measured}")
    return measured


class LiveRemote(Remote):
    def __init__(self):
        super().__init__()
        self.started_with: list[str] = []

    def _backend(self, request) -> dict:
        self.started_with.append(request.config)
        return {"tags": set(), "users": {}}


class BlackHole:
    def __init__(self):
        self.accepted = 0
        self.held = []
        self.server = None

    async def _swallow(self, reader, writer):
        self.accepted += 1
        self.held.append(writer)
        await asyncio.sleep(3600)

    async def open(self) -> int:
        self.server = await asyncio.start_server(self._swallow, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    def close(self) -> None:
        for writer in self.held:
            writer.close()
        self.server.close()


def core_payload(name: str, marker: str) -> dict:
    config = copy.deepcopy(XRAY_CONFIG)
    config["inbounds"][0]["streamSettings"]["xhttpSettings"]["path"] = f"/{marker}"
    return {
        "config": config,
        "name": name,
        "type": "xray",
        "exclude_inbound_tags": [],
        "fallbacks_inbound_tags": ["fallback-A", "fallback-B"],
    }


async def node_row(node_id: int) -> Node | None:
    async with Session() as session:
        return (await session.execute(select(Node).where(Node.id == node_id))).scalar_one_or_none()


async def stops_owed_to(node_id: int) -> int:
    async with Session() as session:
        present = await session.run_sync(lambda sync: inspect(sync.connection()).has_table("node_retirements"))
        if not present:
            return 0
        query = text("SELECT count(*) FROM node_retirements WHERE node_id = :node_id")
        return (await session.execute(query, {"node_id": node_id})).scalar_one()


async def eventually(predicate, timeout: float) -> float | None:
    started = time.monotonic()
    while True:
        if await predicate():
            return time.monotonic() - started
        if time.monotonic() - started >= timeout:
            return None
        await asyncio.sleep(0.1)


class Panel:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, hole: BlackHole, hole_port: int):
        self.monkeypatch = monkeypatch
        self.hole = hole
        self.hole_port = hole_port
        self.remotes: dict[int, LiveRemote] = {}
        self.client: httpx.AsyncClient | None = None
        self.watchers: list[asyncio.Task] = []
        self.real_create_node = app_node.create_node

    def create_node(self, **kwargs):
        remote = self.remotes.get(int(kwargs["node_id"]))
        if remote is None:
            return self.real_create_node(**kwargs)
        real_class = type(self.real_create_node(**kwargs))
        kwargs.pop("connection")
        return _model_class(real_class)(remote, **kwargs)

    async def __aenter__(self):
        patch = self.monkeypatch
        patch.setattr(base, "SessionLocal", Session)
        patch.delitem(app.dependency_overrides, base.get_db, raising=False)
        patch.setattr(node_manager, "_nodes", {})
        patch.setattr(node_manager, "_node_signatures", {})
        patch.setattr(node_manager, "_user_sync_locks", {})
        patch.setattr(node_extras, "_CONNECT_LOCKS", {})
        patch.setattr(app_node, "create_node", self.create_node)
        patch.setattr(NodeOperation, "_in_flight_connects", set())
        patch.setattr(NodeOperation, "_pending_user_sync", set())
        patch.setattr(node_checker, "NODE_CHECK_TIMEOUT", NODE_CHECK_TIMEOUT)
        patch.setattr(node_checker, "_check_tasks", {})
        for name in NOTIFICATIONS:
            patch.setattr(notification, name, AsyncMock(), raising=False)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://panel.test")
        answer = await self.client.post(
            "/api/admin/token", data={"username": "testadmin", "password": "testadmin", "grant_type": "password"}
        )
        assert answer.status_code == 200, answer.text
        self.client.headers["Authorization"] = f"Bearer {answer.json()['access_token']}"
        return self

    async def __aexit__(self, *exc_info):
        for watcher in self.watchers:
            watcher.cancel()
        module = background_connects()
        if module is not None:
            await module.cancel_background_connects()
        for node_id in list((await node_manager.get_nodes()).keys()):
            await node_manager.remove_node(node_id, remote_stop=False)
        await self.client.aclose()
        await asyncio.sleep(0.05)

    async def core(self) -> int:
        name = unique_name("deadnode_core")
        answer = await self.client.post("/api/core", json=core_payload(name, unique_name("before")))
        assert answer.status_code == 201, answer.text
        return answer.json()["id"]

    async def _seed(self, core_id: int, *, port: int, status: NodeStatus, message: str | None) -> Node:
        async with Session() as session:
            row = Node(
                name=unique_name("deadnode"),
                address="127.0.0.1",
                port=port,
                api_port=port,
                server_ca=_ca_pem(),
                api_key=str(uuid4()),
                core_config_id=core_id,
                connection_type=NodeConnectionType.grpc,
                default_timeout=DEAD_DEFAULT_TIMEOUT,
                internal_timeout=DEAD_INTERNAL_TIMEOUT,
                status=status,
            )
            row.message = message
            session.add(row)
            await session.commit()
            return row

    async def dead_node(self, core_id: int, *, health: Health = Health.BROKEN) -> Node:
        row = await self._seed(core_id, port=self.hole_port, status=NodeStatus.error, message=HEALTH_CHECK_PROBE)
        pg_node = await node_manager.update_node(row)
        if health is not Health.NOT_CONNECTED:
            await pg_node.set_health(health)
        return row

    async def just_died_node(self, core_id: int) -> Node:
        row = await self._seed(core_id, port=self.hole_port, status=NodeStatus.connected, message=None)
        pg_node = await node_manager.update_node(row)
        await pg_node.set_health(Health.HEALTHY)
        return row

    async def healthy_node(self, core_id: int) -> tuple[Node, LiveRemote]:
        row = await self._seed(core_id, port=1, status=NodeStatus.connected, message=None)
        remote = LiveRemote()
        self.remotes[row.id] = remote
        pg_node = await node_manager.update_node(row)
        await pg_node.start(config="{}", backend_type=service.BackendType.XRAY, users=[])
        remote.started_with.clear()
        return row, remote

    def watch_status_write(self, node_id: int, written) -> dict:
        seen: dict[str, float] = {}
        started = time.monotonic()

        async def poll():
            while True:
                row = await node_row(node_id)
                if row is not None and written(row):
                    seen["at"] = time.monotonic() - started
                    return
                await asyncio.sleep(0.1)

        self.watchers.append(asyncio.create_task(poll()))
        return seen

    async def timed(self, request) -> tuple[httpx.Response, float]:
        started = time.monotonic()
        answer = await asyncio.wait_for(request, timeout=GIVE_UP_AFTER)
        return answer, time.monotonic() - started


@pytest_asyncio.fixture
async def panel(monkeypatch):
    hole = BlackHole()
    port = await hole.open()
    try:
        async with Panel(monkeypatch, hole, port) as opened:
            yield opened
    finally:
        hole.close()


async def assert_deleted_or_refused_with_reason(answer: httpx.Response, row: Node) -> None:
    kept = await node_row(row.id)
    if answer.status_code in (200, 204):
        assert kept is None, f"the delete answered {answer.status_code} but the row of {row.name} is still there"
        assert await node_manager.get_node(row.id) is None, "the deleted node is still managed"
        assert await stops_owed_to(row.id) == 1, (
            f"{row.name} was deleted without its Stop confirmed and no Stop is owed to it"
        )
        return
    assert answer.status_code in (409, 503), answer.text
    assert kept is not None, "the delete was refused but the row is gone"
    assert row.name in answer.json()["detail"], answer.text


async def test_deleting_a_dead_node_answers_before_its_timeout_and_owes_its_stop(panel):
    core_id = await panel.core()
    dead = await panel.dead_node(core_id)

    answer, elapsed = await panel.timed(panel.client.delete(f"/api/node/{dead.id}"))

    measured = report(
        f"DELETE /api/node/{{id}} of a dead node answered {answer.status_code} after {elapsed:.1f}s "
        f"({load()}, default_timeout={DEAD_DEFAULT_TIMEOUT}s)"
    )
    await assert_deleted_or_refused_with_reason(answer, dead)
    assert elapsed < ANSWER_WITHIN, f"the delete waited for the dead node; {measured}"


async def test_bulk_deleting_a_dead_and_a_healthy_node_answers_before_the_dead_nodes_timeout(panel):
    core_id = await panel.core()
    healthy, remote = await panel.healthy_node(core_id)
    dead = await panel.dead_node(core_id)

    answer, elapsed = await panel.timed(
        panel.client.post("/api/nodes/bulk/delete", json={"ids": [dead.id, healthy.id]})
    )

    measured = report(
        f"POST /api/nodes/bulk/delete of one dead and one healthy node answered {answer.status_code} after "
        f"{elapsed:.1f}s ({load()}, default_timeout={DEAD_DEFAULT_TIMEOUT}s)"
    )
    assert await node_row(healthy.id) is None, answer.text
    assert not remote.running, "the healthy node was deleted but its core was never stopped"
    if answer.status_code == 200:
        assert answer.json()["count"] == 2, answer.text
    await assert_deleted_or_refused_with_reason(answer, dead)
    assert elapsed < ANSWER_WITHIN, f"the bulk delete waited for the dead node; {measured}"


async def admin_restart(panel: Panel, dead: Node) -> asyncio.Task:
    return asyncio.create_task(panel.client.post(f"/api/node/{dead.id}/reconnect"))


HOLDERS = {
    "admin_restart": (admin_restart, Health.BROKEN),
}


@pytest.mark.parametrize("holder", list(HOLDERS))
async def test_deleting_a_dead_node_does_not_wait_for_a_reconnect_that_holds_its_connect_lock(panel, holder):
    start_holder, health = HOLDERS[holder]
    core_id = await panel.core()
    dead = await panel.dead_node(core_id, health=health)
    holding = await start_holder(panel, dead)
    locked_after = await eventually(lambda: _locked(dead.id), timeout=ANSWER_WITHIN)
    assert locked_after is not None, f"the {holder} never took the connect lock"
    held_since = time.monotonic()

    answer, elapsed = await panel.timed(panel.client.delete(f"/api/node/{dead.id}"))
    lock_held = time.monotonic() - held_since if not holding.done() else None
    await asyncio.wait_for(asyncio.gather(holding, return_exceptions=True), timeout=GIVE_UP_AFTER)

    measured = report(
        f"{holder}: delete took {elapsed:.1f}s while the connect lock was held "
        f"({load()}, default_timeout={DEAD_DEFAULT_TIMEOUT}s, NODE_CHECK_TIMEOUT={NODE_CHECK_TIMEOUT}s"
        f"{'' if lock_held is None else f', holder still running {lock_held:.1f}s later'})"
    )
    await assert_deleted_or_refused_with_reason(answer, dead)
    assert elapsed < ANSWER_WITHIN, f"the delete waited for the reconnect; {measured}"


async def _locked(node_id: int) -> bool:
    return node_extras._connect_lock(node_id).locked()


async def test_deleting_a_node_does_not_wait_for_a_full_user_sync_stuck_on_it(panel):
    core_id = await panel.core()
    dead = await panel.dead_node(core_id, health=Health.HEALTHY)
    async with Session() as session:
        row = (await session.execute(select(Node).where(Node.id == dead.id))).scalar_one()
        row.status = NodeStatus.connected
        await session.commit()
    syncing = asyncio.create_task(panel.client.put(f"/api/node/{dead.id}/sync"))
    sync_lock_taken = await eventually(lambda: _sync_lock_held(dead.id), timeout=ANSWER_WITHIN)
    assert sync_lock_taken is not None, "the node sync never took the node's user-sync lock"

    answer, elapsed = await panel.timed(panel.client.delete(f"/api/node/{dead.id}"))
    sync_answer = await asyncio.wait_for(syncing, timeout=GIVE_UP_AFTER)

    measured = report(
        f"delete took {elapsed:.1f}s behind PUT /api/node/{{id}}/sync ({sync_answer.status_code}) "
        f"({load()}, default_timeout={DEAD_DEFAULT_TIMEOUT}s)"
    )
    await assert_deleted_or_refused_with_reason(answer, dead)
    assert elapsed < ANSWER_WITHIN, f"the delete waited for the user sync to the dead node; {measured}"


async def _sync_lock_held(node_id: int) -> bool:
    lock = node_manager._user_sync_locks.get(node_id)
    return lock is not None and lock.locked()


async def test_the_dashboards_realtime_stats_do_not_wait_for_a_node_that_just_died(panel):
    core_id = await panel.core()
    await panel.dead_node(core_id, health=Health.HEALTHY)

    answer, elapsed = await panel.timed(panel.client.get("/api/nodes/realtime_stats"))

    measured = report(
        f"GET /api/nodes/realtime_stats took {elapsed:.1f}s ({load()}, default_timeout={DEAD_DEFAULT_TIMEOUT}s)"
    )
    assert answer.status_code == 200, answer.text
    assert elapsed < ANSWER_WITHIN, (
        f"the dashboard poll waited for a node the health check has not flagged yet; {measured}"
    )


def healthy_status_written(row: Node) -> bool:
    return row.status == NodeStatus.connected and row.xray_version == CORE_VERSION


def dead_status_written(row: Node) -> bool:
    return row.status == NodeStatus.error and row.message != HEALTH_CHECK_PROBE


async def _written(node_id: int, written) -> bool:
    row = await node_row(node_id)
    return row is not None and written(row)


async def _restarted(remote: LiveRemote) -> bool:
    return bool(remote.started_with)


def edit_core_and_restart(panel: Panel, core_id: int, marker: str):
    payload = core_payload(unique_name("deadnode_core"), marker)
    return panel.client.put(f"/api/core/{core_id}", params={"restart_nodes": "true"}, json=payload)


RESTARTS = {
    "core_edit_with_restart": lambda panel, core_id, ids, marker: edit_core_and_restart(panel, core_id, marker),
    "core_restart": lambda panel, core_id, ids, marker: panel.client.post(f"/api/core/{core_id}/restart"),
    "nodes_bulk_reconnect": lambda panel, core_id, ids, marker: panel.client.post(
        "/api/nodes/bulk/reconnect", json={"ids": ids}
    ),
    "nodes_reconnect_by_core": lambda panel, core_id, ids, marker: panel.client.post(
        "/api/nodes/reconnect", params={"core_id": core_id}
    ),
}


def assert_restart_answer(endpoint: str, answer: httpx.Response, core_id: int, names: list[str]) -> None:
    if endpoint == "core_edit_with_restart":
        assert answer.status_code == 200, answer.text
        assert answer.json()["id"] == core_id, answer.text
    elif endpoint == "core_restart":
        assert answer.status_code == 204, answer.text
        assert answer.content == b"", answer.text
    elif endpoint == "nodes_bulk_reconnect":
        assert answer.status_code == 200, answer.text
        assert sorted(answer.json()["nodes"]) == sorted(names), answer.text
        assert answer.json()["count"] == len(names), answer.text
    else:
        assert answer.status_code == 200, answer.text
        assert answer.json() == {}, answer.text


@pytest.mark.parametrize("endpoint", list(RESTARTS))
async def test_restarting_a_core_with_a_dead_node_answers_promptly_and_still_restarts_both(panel, endpoint):
    core_id = await panel.core()
    healthy, remote = await panel.healthy_node(core_id)
    dead = await panel.dead_node(core_id)
    marker = unique_name("after")
    healthy_written = panel.watch_status_write(healthy.id, healthy_status_written)

    answer, elapsed = await panel.timed(RESTARTS[endpoint](panel, core_id, [dead.id, healthy.id], marker))
    dead_written_after = await eventually(lambda: _written(dead.id, dead_status_written), timeout=DEAD_RESTART_BUDGET)
    await asyncio.sleep(0.2)

    measured = report(
        f"{endpoint}: request {elapsed:.1f}s, healthy status written at "
        f"{healthy_written.get('at', float('nan')):.1f}s, dead status written "
        f"{'never' if dead_written_after is None else f'{elapsed + dead_written_after:.1f}s'} after the request "
        f"started ({load()}, default_timeout={DEAD_DEFAULT_TIMEOUT}s)"
    )
    assert_restart_answer(endpoint, answer, core_id, [dead.name, healthy.name])
    assert remote.started_with, f"the healthy node was never restarted; {measured}"
    if endpoint == "core_edit_with_restart":
        assert marker in remote.started_with[-1], "the healthy node was restarted with the old core config"
    assert dead_written_after is not None, f"the dead node's restart outcome was never written; {measured}"
    assert "at" in healthy_written and healthy_written["at"] < ANSWER_WITHIN, (
        f"the healthy node's status waited for the dead node; {measured}"
    )
    assert elapsed < ANSWER_WITHIN, f"the request waited for the dead node; {measured}"


async def test_reconnecting_a_single_dead_node_answers_before_its_timeout(panel):
    core_id = await panel.core()
    dead = await panel.dead_node(core_id)

    answer, elapsed = await panel.timed(panel.client.post(f"/api/node/{dead.id}/reconnect"))
    dead_written_after = await eventually(lambda: _written(dead.id, dead_status_written), timeout=DEAD_RESTART_BUDGET)

    measured = report(
        f"POST /api/node/{{id}}/reconnect of a dead node: request {elapsed:.1f}s "
        f"({load()}, default_timeout={DEAD_DEFAULT_TIMEOUT}s)"
    )
    assert answer.status_code == 200, answer.text
    assert answer.json() == {}, answer.text
    assert dead_written_after is not None, f"the reconnect outcome was never written; {measured}"
    assert elapsed < ANSWER_WITHIN, f"POST /api/node/{{id}}/reconnect waited for the dead node; {measured}"


@pytest.mark.parametrize("dead_count", [node_operation.CONNECT_CONCURRENCY, node_operation.CONNECT_CONCURRENCY + 2])
@pytest.mark.parametrize("dead_rows", ["error", "connected"])
async def test_a_core_restart_reaches_a_healthy_node_behind_a_full_slate_of_dead_ones(panel, dead_rows, dead_count):
    core_id = await panel.core()
    for _ in range(dead_count):
        if dead_rows == "error":
            await panel.dead_node(core_id)
        else:
            await panel.just_died_node(core_id)
    healthy, remote = await panel.healthy_node(core_id)
    healthy_written = panel.watch_status_write(healthy.id, healthy_status_written)

    restart = asyncio.create_task(panel.timed(panel.client.post(f"/api/core/{core_id}/restart")))
    reached = await eventually(lambda: _restarted(remote), timeout=GIVE_UP_AFTER)
    answer, elapsed = await restart
    await eventually(lambda: _written(healthy.id, healthy_status_written), timeout=GIVE_UP_AFTER)
    await asyncio.sleep(0.3)

    measured = report(
        f"{dead_count} dead nodes with {dead_rows} rows: request {elapsed:.1f}s, healthy Start sent "
        f"{'never' if reached is None else f'{reached:.1f}s'}, healthy status written "
        f"{healthy_written.get('at', float('nan')):.1f}s ({load()}, default_timeout={DEAD_DEFAULT_TIMEOUT}s)"
    )
    assert answer.status_code == 204, answer.text
    assert reached is not None and reached < ANSWER_WITHIN, measured
    assert reached < DEAD_INTERNAL_TIMEOUT, f"the healthy node's Start waited for the dead nodes' probes; {measured}"
    assert "at" in healthy_written and healthy_written["at"] < ANSWER_WITHIN, measured
    assert elapsed < ANSWER_WITHIN, measured
