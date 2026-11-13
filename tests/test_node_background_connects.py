import asyncio
import json
import logging
import random
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiorwlock import RWLock
from PasarGuardNodeBridge import NodeAPIError
from PasarGuardNodeBridge.storage import get_default_lifecycle_coordinator
from sqlalchemy import select

import app.node as app_node
from app.core.manager import core_manager
from app.db.models import Node, NodeConnectionType, NodeStatus
from app.fork.operation import node_background_connects, node_extras
from app.fork.operation.node_connect_generation import bump_connect_generation
from app.jobs import node_checker
from app.models.admin import AdminDetails
from app.models.core import CoreType
from app.models.node import BulkNodeSelection, NodeModify
from app.models.settings import General, NodeHealthSettings
from app.node import node_manager
from app.operation import OperatorType, node as node_operation
from app.operation.node import NodeOperation
from tests.api.helpers import unique_name
from tests.test_node_connect_stale_snapshot import Remote, _ca_pem, _core, _model_class
from tests.test_node_delete_stop_contract import Session, SessionOfItsOwn

INTERNAL_TIMEOUT = 3
REQUEST_BOUND = 6.0
SETTLE = 12.0
ADMIN = AdminDetails(username="u2-background")


class GatedRemote(Remote):
    def __init__(self):
        super().__init__()
        self.gates: dict[str, asyncio.Event] = {}
        self.dead = False
        self.started_with: list[list[str]] = []
        self.start_outcomes: list[bool] = []
        self.start_attempts = 0
        self.arrived: dict[str, int] = {}

    def hold(self, rpc: str) -> None:
        self.gates[rpc] = asyncio.Event()

    def release(self, rpc: str) -> None:
        gate = self.gates.pop(rpc, None)
        if gate is not None:
            gate.set()

    async def rpc(self, name: str, request):
        self.arrived[name] = self.arrived.get(name, 0) + 1
        gate = self.gates.get(name)
        if gate is not None:
            await gate.wait()
        if name == "Start":
            self.start_attempts += 1
        if self.dead:
            raise NodeAPIError(-1, "Request timed out")
        if name == "Start":
            if self.start_outcomes and not self.start_outcomes.pop(0):
                raise NodeAPIError(-1, "Request timed out")
            self.started_with.append(json.loads(request.config)["tags"])
        return await super().rpc(name, request)


class Fleet:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, shared: bool = False):
        self.monkeypatch = monkeypatch
        self.shared = shared
        self.remotes: dict[int, GatedRemote] = {}
        self.tags = [unique_name("bg_a")]
        self.real_create_node = app_node.create_node
        self.published: list[tuple[int, NodeStatus | None]] = []
        self.operation: NodeOperation | None = None
        self.node_ids: list[int] = []

    def create_node(self, **kwargs):
        remote = self.remotes[int(kwargs["node_id"])]
        real_class = type(self.real_create_node(**kwargs))
        kwargs.pop("connection")
        return _model_class(real_class)(remote, **kwargs)

    async def _publish(self, action: str, node_id: int) -> None:
        row = await self.row(node_id)
        self.published.append((node_id, None if row is None else row.status))

    async def __aenter__(self):
        patch = self.monkeypatch
        patch.setattr(node_manager, "_nodes", {})
        patch.setattr(node_manager, "_node_signatures", {})
        patch.setattr(node_manager, "_user_sync_locks", {})
        patch.setattr(node_manager, "_lock", RWLock(fast=True))
        patch.setattr(node_extras, "_CONNECT_LOCKS", {})
        patch.setattr(app_node, "create_node", self.create_node)
        patch.setattr(core_manager, "get_cores", AsyncMock(side_effect=self._cores))
        patch.setattr(node_operation, "GetDB", SessionOfItsOwn)
        patch.setattr(node_extras, "GetDB", SessionOfItsOwn)
        patch.setattr(NodeOperation, "_in_flight_connects", set())
        patch.setattr(NodeOperation, "_pending_user_sync", set())
        for name in ("connect_node", "error_node", "modify_node", "remove_node"):
            patch.setattr(node_operation.notification, name, AsyncMock())
        patch.setattr(node_extras.notification, "error_node", AsyncMock())
        from app.fork.operation import node_retirement

        patch.setattr(node_retirement, "GetDB", SessionOfItsOwn)
        patch.setattr(node_retirement, "needs_shared_bridge_memory", lambda: self.shared)
        patch.setattr(node_retirement, "publish_node_sync", AsyncMock())
        patch.setattr(node_operation, "needs_shared_bridge_memory", lambda: self.shared)
        patch.setattr(node_operation, "publish_node_sync", self._publish)
        patch.setattr(node_background_connects, "needs_shared_bridge_memory", lambda: self.shared)
        patch.setattr(node_background_connects, "publish_node_sync", self._publish)
        patch.setattr(node_checker, "_owns_health_checks", lambda: True)
        patch.setattr(node_checker, "needs_shared_bridge_memory", lambda: False)
        patch.setattr(node_checker, "hold_for_usage_collection", AsyncMock(return_value=False))
        patch.setattr(node_checker, "after_healthy_node_check", AsyncMock())
        patch.setattr(node_checker, "GetDB", SessionOfItsOwn)
        health = NodeHealthSettings.model_construct(
            fail_threshold=1, recover_threshold=1, probe_timeout=INTERNAL_TIMEOUT, max_backoff=60
        )
        patch.setattr(
            node_checker, "general_settings", AsyncMock(return_value=General.model_construct(node_health=health))
        )
        for state in ("_health_streaks", "_probe_backoffs", "_core_starting_since", "_version_refresh_deadlines"):
            patch.setattr(node_checker, state, {})
        self.operation = NodeOperation(operator_type=OperatorType.API)
        return self

    async def __aexit__(self, *exc_info):
        for remote in self.remotes.values():
            for rpc in list(remote.gates):
                remote.release(rpc)
        await node_background_connects.cancel_background_connects()
        await asyncio.sleep(0.1)
        for node_id in self.node_ids:
            await node_manager.remove_node(node_id, remote_stop=False)
            async with Session() as session:
                row = await session.get(Node, node_id)
                if row is not None:
                    await session.delete(row)
                    await session.commit()

    async def _cores(self, ids):
        return {core_id: _core(CoreType.xray, list(self.tags)) for core_id in ids}

    async def node(self, *, running: bool = True) -> int:
        async with Session() as session:
            row = Node(
                name=unique_name("bg_node"),
                address="127.0.0.1",
                port=1,
                api_port=1,
                server_ca=_ca_pem(),
                api_key=str(uuid4()),
                core_config_id=None,
                connection_type=NodeConnectionType.grpc,
                default_timeout=5,
                internal_timeout=INTERNAL_TIMEOUT,
                status=NodeStatus.connected,
            )
            session.add(row)
            await session.commit()
            node_id = row.id
        self.node_ids.append(node_id)
        remote = self.remotes[node_id] = GatedRemote()
        bridge = await node_manager.update_node(await self.row(node_id))
        if running:
            await bridge.start(config=json.dumps({"tags": self.tags}), backend_type=0, users=[])
            remote.started_with.clear()
            remote.start_attempts = 0
        return node_id

    async def row(self, node_id: int) -> Node | None:
        async with Session() as session:
            return (await session.execute(select(Node).where(Node.id == node_id))).scalar_one_or_none()

    async def status(self, node_id: int) -> NodeStatus | None:
        row = await self.row(node_id)
        return None if row is None else row.status

    async def set_status(self, node_id: int, status: NodeStatus, message: str = "") -> None:
        async with Session() as session:
            row = await session.get(Node, node_id)
            row.status = status
            row.message = message
            await session.commit()

    async def restart(self, node_id: int, tags: list[str] | None = None) -> float:
        if tags is not None:
            self.tags = tags
        started = time.monotonic()
        async with Session() as db:
            await asyncio.wait_for(self.operation.restart_node(db, node_id, ADMIN), timeout=SETTLE)
        return time.monotonic() - started

    async def reconnect(self, node_id: int) -> float:
        started = time.monotonic()
        async with Session() as db:
            await asyncio.wait_for(self.operation.connect_single_node(db, node_id), timeout=SETTLE)
        return time.monotonic() - started

    async def bulk_restart(self, node_ids: list[int]) -> float:
        started = time.monotonic()
        async with Session() as db:
            await asyncio.wait_for(
                self.operation.bulk_restart_nodes(db, BulkNodeSelection(ids=set(node_ids)), ADMIN), timeout=SETTLE
            )
        return time.monotonic() - started

    async def check(self, node_id: int) -> None:
        await node_checker._bounded_health_check(await self.row(node_id), await node_manager.get_node(node_id))

    async def settle(self, predicate) -> bool:
        deadline = time.monotonic() + SETTLE
        while time.monotonic() < deadline:
            if await predicate():
                return True
            await asyncio.sleep(0.05)
        return await predicate()

    async def background_done(self) -> None:
        await self.settle(_no_background)


async def _no_background() -> bool:
    return not node_background_connects._background


async def _arrived(remote: GatedRemote, rpc: str) -> bool:
    return remote.arrived.get(rpc, 0) > 0


async def _queue_on_connect_lock(node_id: int) -> tuple[asyncio.Lock, asyncio.Task]:
    lock = node_extras._connect_lock(node_id)
    taken = asyncio.create_task(lock.acquire())
    await asyncio.sleep(0)
    return lock, taken


async def test_a_probe_that_began_before_a_late_result_was_written_does_not_overwrite_it(monkeypatch):
    async with Fleet(monkeypatch) as fleet:
        node_id = await fleet.node()
        remote = fleet.remotes[node_id]
        remote.hold("Start")
        took = await fleet.restart(node_id, [unique_name("bg_b")])
        assert took < REQUEST_BOUND, f"the restart waited {took:.1f}s for a held Start"

        lock, taken = await _queue_on_connect_lock(node_id)
        remote.release("Start")
        await asyncio.wait_for(taken, timeout=SETTLE)
        remote.hold("GetBackendStats")
        probe = asyncio.create_task(fleet.check(node_id))
        await asyncio.sleep(0.2)
        lock.release()
        await fleet.background_done()
        assert await fleet.status(node_id) is NodeStatus.connected

        remote.dead = True
        remote.release("GetBackendStats")
        await asyncio.wait_for(probe, timeout=SETTLE)

        assert await fleet.status(node_id) is NodeStatus.connected, "a probe older than the late result overwrote it"


async def test_a_probe_that_began_after_a_late_result_was_written_is_recorded(monkeypatch):
    async with Fleet(monkeypatch) as fleet:
        node_id = await fleet.node()
        remote = fleet.remotes[node_id]
        remote.hold("Start")
        await fleet.restart(node_id, [unique_name("bg_b")])
        remote.release("Start")
        await fleet.background_done()
        assert await fleet.status(node_id) is NodeStatus.connected

        remote.dead = True
        await fleet.check(node_id)

        assert await fleet.status(node_id) is NodeStatus.error, "a current probe error was dropped"


async def test_a_node_that_dies_before_its_held_start_answers_is_recorded_as_error(monkeypatch):
    async with Fleet(monkeypatch) as fleet:
        node_id = await fleet.node()
        remote = fleet.remotes[node_id]
        remote.hold("Start")
        took = await fleet.restart(node_id, [unique_name("bg_b")])
        assert took < REQUEST_BOUND

        remote.dead = True
        remote.release("Start")
        await fleet.background_done()

        assert await fleet.status(node_id) is NodeStatus.error, "the late failed Start was never recorded"


async def test_a_node_that_dies_after_its_start_and_before_the_late_write_ends_as_error(monkeypatch):
    async with Fleet(monkeypatch) as fleet:
        node_id = await fleet.node()
        remote = fleet.remotes[node_id]
        await fleet.set_status(node_id, NodeStatus.error, "before the restart")
        remote.hold("Start")
        await fleet.restart(node_id, [unique_name("bg_b")])

        lock, taken = await _queue_on_connect_lock(node_id)
        remote.release("Start")
        await asyncio.wait_for(taken, timeout=SETTLE)
        remote.dead = True
        lock.release()
        await fleet.background_done()
        assert await fleet.status(node_id) is NodeStatus.connected, "the late Start result was not written"

        await fleet.check(node_id)

        assert await fleet.status(node_id) is NodeStatus.error, "the node died after its Start but stays connected"


async def test_a_late_result_never_overwrites_a_restart_that_began_after_it(monkeypatch):
    async with Fleet(monkeypatch) as fleet:
        node_id = await fleet.node()
        remote = fleet.remotes[node_id]
        remote.hold("Start")

        async def first_start_fails():
            raise NodeAPIError(-1, "Request timed out")

        remote.on("Start", first_start_fails)
        remote.arrived.clear()
        first = asyncio.create_task(fleet.restart(node_id))
        assert await fleet.settle(lambda: _arrived(remote, "Start")), "the first restart never reached its Start"
        second = asyncio.create_task(fleet.restart(node_id))
        took = await asyncio.wait_for(asyncio.gather(first, second), timeout=SETTLE)
        assert max(took) < REQUEST_BOUND, f"the restarts waited {took} for a held Start"

        remote.release("Start")
        await fleet.background_done()

        assert await fleet.status(node_id) is NodeStatus.connected
        assert remote.running
        node_operation.notification.error_node.assert_not_called()


async def test_a_late_result_never_overwrites_a_newer_restart_that_answered_in_time(monkeypatch):
    async with Fleet(monkeypatch) as fleet:
        node_id = await fleet.node()
        remote = fleet.remotes[node_id]
        remote.hold("Start")

        async def first_start_fails():
            raise NodeAPIError(-1, "Request timed out")

        remote.on("Start", first_start_fails)
        late_writes: list[NodeStatus] = []
        real_write = NodeOperation._update_single_node_status

        async def recording_write(db, written_id, status, *args, **kwargs):
            written = await real_write(db, written_id, status, *args, **kwargs)
            if written and written_id == node_id:
                late_writes.append(status)
            return written

        monkeypatch.setattr(NodeOperation, "_update_single_node_status", staticmethod(recording_write))
        first_took = await fleet.restart(node_id)
        assert first_took < REQUEST_BOUND, f"the first restart waited {first_took:.1f}s for its held Start"

        remote.arrived.clear()
        second = asyncio.create_task(fleet.restart(node_id))
        await asyncio.sleep(0.2)
        remote.release("Start")
        second_took = await asyncio.wait_for(second, timeout=SETTLE)
        await fleet.background_done()

        assert second_took < INTERNAL_TIMEOUT, f"the newer restart did not answer in time ({second_took:.1f}s)"
        assert await fleet.status(node_id) is NodeStatus.connected
        assert late_writes == [], f"the older restart's late result was written after the newer one: {late_writes}"


async def test_shutdown_cancels_background_connects_without_writing(monkeypatch, caplog):
    async with Fleet(monkeypatch) as fleet:
        node_id = await fleet.node()
        remote = fleet.remotes[node_id]
        await fleet.set_status(node_id, NodeStatus.error, "before the restart")
        remote.hold("Start")
        await fleet.restart(node_id, [unique_name("bg_b")])
        assert node_background_connects._background

        with caplog.at_level(logging.ERROR):
            await node_background_connects.cancel_background_connects()
            remote.release("Start")
            await asyncio.sleep(0.5)

        assert not node_background_connects._background
        assert await fleet.status(node_id) is NodeStatus.error, "a cancelled background connect still wrote"
        assert not [record for record in caplog.records if "late connect result" in record.getMessage()]


async def test_shared_mode_publishes_a_node_only_after_its_connect_finished(monkeypatch):
    async with Fleet(monkeypatch, shared=True) as fleet:
        held = await fleet.node()
        fast = await fleet.node()
        fleet.remotes[held].hold("Start")
        await fleet.set_status(held, NodeStatus.error, "before the restart")

        took = await fleet.bulk_restart([held, fast])
        assert took < REQUEST_BOUND
        assert [node_id for node_id, _ in fleet.published] == [fast]

        fleet.remotes[held].release("Start")
        await fleet.background_done()

        assert fleet.published[-1] == (held, NodeStatus.connected), (
            f"the held node was published before its late result was written: {fleet.published}"
        )


async def test_a_late_result_is_skipped_when_another_process_moved_the_shared_lifecycle(monkeypatch):
    async with Fleet(monkeypatch, shared=True) as fleet:
        node_id = await fleet.node()
        remote = fleet.remotes[node_id]
        await fleet.set_status(node_id, NodeStatus.error, "before the restart")
        remote.hold("Start")
        await fleet.restart(node_id, [unique_name("bg_b")])

        kwargs = node_manager._create_node_kwargs(await fleet.row(node_id))
        kwargs.setdefault("lifecycle_coordinator", get_default_lifecycle_coordinator())
        kwargs["worker_id"] = "another-process"
        other = fleet.create_node(**kwargs)
        with pytest.raises(NodeAPIError) as refused:
            await other.start(config=json.dumps({"tags": fleet.tags}), backend_type=0, users=[])
        assert refused.value.code == 409

        lock, taken = await _queue_on_connect_lock(node_id)
        remote.release("Start")
        await asyncio.wait_for(taken, timeout=SETTLE)
        remote.dead = True
        with pytest.raises(NodeAPIError):
            await other.start(config=json.dumps({"tags": fleet.tags}), backend_type=0, users=[])
        remote.dead = False
        lock.release()
        await fleet.background_done()

        assert await fleet.status(node_id) is NodeStatus.error, (
            "a late result was written after another process started the node"
        )


async def test_answering_nodes_never_queue_behind_unanswering_ones_and_both_caps_hold(monkeypatch):
    unanswering = set(range(1, 17))
    answering = set(range(17, 25))
    peaks = {"all": 0, "unanswered": 0}
    running = {"all": 0, "unanswered": 0}
    started_at: dict[int, float] = {}
    loop = asyncio.get_running_loop()
    began = loop.time()

    async def answers(node):
        if node.id in unanswering:
            await asyncio.sleep(0.3)
            return False
        return True

    async def connect(db_node, core, users, extra_cores=None, *, force_start=False, stop_first=True):
        started_at[db_node.id] = loop.time() - began
        kinds = ["all", "unanswered"] if db_node.id in unanswering else ["all"]
        for kind in kinds:
            running[kind] += 1
            peaks[kind] = max(peaks[kind], running[kind])
        await asyncio.sleep(0.5 if db_node.id in unanswering else 0.05)
        for kind in kinds:
            running[kind] -= 1
        return {
            "node_id": db_node.id,
            "status": NodeStatus.connected,
            "message": "",
            "xray_version": "",
            "node_version": "",
            "old_status": NodeStatus.connected,
        }

    nodes = [
        SimpleNamespace(
            id=i, status=NodeStatus.connected, core_config_id=1, additional_core_config_ids=None, name=f"n{i}"
        )
        for i in sorted(unanswering | answering)
    ]
    by_id = {node.id: node for node in nodes}
    monkeypatch.setattr(node_background_connects, "_answers", answers)
    monkeypatch.setattr(NodeOperation, "connect_node", staticmethod(connect))
    monkeypatch.setattr(NodeOperation, "_register_if_wanted", AsyncMock(return_value=True))
    monkeypatch.setattr(NodeOperation, "_get_core_users_map", AsyncMock(return_value=({1: object()}, {1: []})))
    monkeypatch.setattr(
        node_operation, "get_node_by_id", AsyncMock(side_effect=lambda db, node_id, **kwargs: by_id.get(node_id))
    )

    written = AsyncMock()
    monkeypatch.setattr(node_operation, "bulk_update_node_status", written)

    operation = NodeOperation.__new__(NodeOperation)
    await operation._connect_nodes_bulk_local(object(), nodes)

    assert not node_background_connects.connecting_in_background()
    assert sorted(result["node_id"] for result in written.await_args.args[1]) == sorted(by_id)
    assert max(started_at[node_id] for node_id in answering) < 0.25, (
        f"an answering node waited behind the unanswering ones: {sorted(started_at.items())}"
    )
    assert peaks["all"] <= node_operation.CONNECT_CONCURRENCY
    assert peaks["unanswered"] <= node_operation.CONNECT_CONCURRENCY // 2
    assert peaks["unanswered"] > 1


async def test_disabling_a_node_withdraws_its_running_restart_instead_of_waiting(monkeypatch):
    async with Fleet(monkeypatch) as fleet:
        node_id = await fleet.node()
        remote = fleet.remotes[node_id]
        remote.hold("Start")
        await fleet.restart(node_id, [unique_name("bg_b")])

        started = time.monotonic()
        async with Session() as db:
            await asyncio.wait_for(
                fleet.operation.modify_node(db, node_id, NodeModify(status=NodeStatus.disabled), ADMIN),
                timeout=SETTLE,
            )
        took = time.monotonic() - started
        remote.release("Start")
        await fleet.background_done()

        assert took < REQUEST_BOUND, f"disabling waited {took:.1f}s behind the running restart"
        assert await fleet.status(node_id) is NodeStatus.disabled
        assert await node_manager.get_node(node_id) is None


@pytest.mark.parametrize("seed", range(12))
async def test_concurrent_restarts_and_reconnects_neither_delay_live_nodes_nor_lose_or_repeat_a_connect(
    monkeypatch, seed
):
    rng = random.Random(seed)
    async with Fleet(monkeypatch) as fleet:
        live = [await fleet.node() for _ in range(rng.randint(1, 3))]
        dead = [await fleet.node() for _ in range(rng.randint(1, 2))]
        hung = [await fleet.node() for _ in range(rng.randint(1, 3))]
        outcome = {node_id: rng.choice([NodeStatus.connected, NodeStatus.error]) for node_id in hung}
        for node_id in dead:
            fleet.remotes[node_id].dead = True
        for node_id in hung:
            fleet.remotes[node_id].hold("Start")
            fleet.remotes[node_id].start_outcomes = [outcome[node_id] is NodeStatus.connected] * 8
        requests = [("bulk", live + dead + hung)]
        requests += [("restart", node_id) for node_id in rng.sample(live + dead + hung, k=rng.randint(1, 4))]
        requests += [("reconnect", node_id) for node_id in rng.sample(live + dead, k=rng.randint(0, 2))]
        rng.shuffle(requests)
        asked = dict.fromkeys(live + dead + hung, 0)
        for kind, target in requests:
            for node_id in target if kind == "bulk" else [target]:
                asked[node_id] += 1

        executed = dict.fromkeys(asked, 0)
        forced = dict.fromkeys(asked, 0)
        dropped = dict.fromkeys(asked, 0)
        late_writes: list[tuple[int, NodeStatus]] = []
        real_inner = NodeOperation._connect_node_inner
        real_superseded = node_operation.connect_snapshot_superseded
        real_write = NodeOperation._update_single_node_status

        async def counted_inner(db_node, core, users, extra_cores, force_start, *args, **kwargs):
            executed[db_node.id] += 1
            forced[db_node.id] += bool(force_start)
            return await real_inner(db_node, core, users, extra_cores, force_start, *args, **kwargs)

        def counted_superseded(node_id, snapshot):
            superseded = real_superseded(node_id, snapshot)
            dropped[node_id] += superseded
            return superseded

        async def recording_write(db, node_id, status, *args, **kwargs):
            written = await real_write(db, node_id, status, *args, **kwargs)
            if written:
                late_writes.append((node_id, status))
            return written

        monkeypatch.setattr(NodeOperation, "_connect_node_inner", staticmethod(counted_inner))
        monkeypatch.setattr(node_operation, "connect_snapshot_superseded", counted_superseded)
        monkeypatch.setattr(NodeOperation, "_update_single_node_status", staticmethod(recording_write))

        def send(kind, target):
            if kind == "bulk":
                return fleet.bulk_restart(target)
            if kind == "restart":
                return fleet.restart(target)
            return fleet.reconnect(target)

        took = await asyncio.wait_for(
            asyncio.gather(*(send(kind, target) for kind, target in requests)), timeout=SETTLE
        )

        assert max(took) < REQUEST_BOUND, f"seed {seed}: a request waited {max(took):.1f}s for a hung node"
        for node_id in live:
            assert await fleet.status(node_id) is NodeStatus.connected, f"seed {seed}: live node {node_id} was delayed"
        for node_id in dead:
            assert await fleet.status(node_id) is NodeStatus.error, f"seed {seed}: dead node {node_id} was not recorded"

        for node_id in hung:
            fleet.remotes[node_id].release("Start")
        await fleet.background_done()

        for node_id, wanted in asked.items():
            assert executed[node_id] + dropped[node_id] == wanted, (
                f"seed {seed}: node {node_id} was asked {wanted} connects, ran {executed[node_id]} "
                f"and dropped {dropped[node_id]} as superseded"
            )
            starts = fleet.remotes[node_id].start_attempts
            assert forced[node_id] <= starts <= executed[node_id], (
                f"seed {seed}: node {node_id} got {starts} Starts for {executed[node_id]} connects, "
                f"{forced[node_id]} of them forced"
            )
        for node_id in hung:
            assert fleet.remotes[node_id].start_attempts == executed[node_id], (
                f"seed {seed}: hung node {node_id} ran {executed[node_id]} connects but got "
                f"{fleet.remotes[node_id].start_attempts} Starts"
            )
        for node_id in hung:
            assert await fleet.status(node_id) is outcome[node_id], f"seed {seed}: hung node {node_id} ended wrong"
            recorded = [status for written_id, status in late_writes if written_id == node_id]
            assert recorded == [outcome[node_id]], (
                f"seed {seed}: hung node {node_id} late results recorded as {recorded}"
            )
        assert not [written_id for written_id, _ in late_writes if written_id in live + dead], (
            f"seed {seed}: a node that answered within the wait was written late: {late_writes}"
        )


async def _late_unit(node_id: int) -> asyncio.Task:
    async def connect():
        node_background_connects.remember_connect_mark(await node_background_connects.connect_mark(node_id))
        return {"status": NodeStatus.connected}

    unit = node_background_connects.start_connect(node_id, connect())
    await unit
    return unit


async def test_a_late_result_is_skipped_once_a_newer_connect_already_finished_or_is_still_pending():
    node_id = 990_000 + random.randrange(10_000)

    alone = await _late_unit(node_id)
    assert await node_background_connects._still_latest(node_id, alone)

    finished_after = await _late_unit(node_id)
    bump_connect_generation(node_id)
    assert not await node_background_connects._still_latest(node_id, finished_after)

    pending_after = await _late_unit(node_id)
    gate = asyncio.Event()
    newer = node_background_connects.start_connect(node_id, gate.wait())
    try:
        assert not await node_background_connects._still_latest(node_id, pending_after)
    finally:
        gate.set()
        await newer
    assert await node_background_connects._still_latest(node_id, pending_after)
