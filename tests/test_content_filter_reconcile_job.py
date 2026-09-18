import asyncio
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.db.models import CoreConfig, CoreType, Node, NodeStatus
from app.fork.content_filter import service
from app.fork.jobs import content_filter_reconcile as job, node_extras
from app.fork.models.content_filter import ContentFilterAssignment, ContentFilterProfile
from app.fork.traffic_log import collector
from tests.api import GetTestDB


@pytest.fixture(autouse=True)
def _fresh_module_state():
    job._in_flight.clear()
    job._pending.clear()
    job._last_status_change.clear()
    job._sem = None
    job._sem_loop = None
    yield
    job._in_flight.clear()
    job._pending.clear()
    job._last_status_change.clear()
    job._sem = None
    job._sem_loop = None


@pytest.fixture(autouse=True)
async def _own_rows_only():
    async with GetTestDB() as db:
        await db.execute(ContentFilterAssignment.__table__.delete())
        await db.execute(ContentFilterProfile.__table__.delete())
        node_watermark = (await db.scalar(select(func.max(Node.id)))) or 0
        core_watermark = (await db.scalar(select(func.max(CoreConfig.id)))) or 0
        await db.commit()
    yield
    async with GetTestDB() as db:
        await db.execute(ContentFilterAssignment.__table__.delete())
        await db.execute(ContentFilterProfile.__table__.delete())
        await db.execute(Node.__table__.delete().where(Node.id > node_watermark))
        await db.execute(CoreConfig.__table__.delete().where(CoreConfig.id > core_watermark))
        await db.commit()


async def _core(db, *tags: str) -> int:
    core = CoreConfig(
        name=f"cf-core-{uuid4().hex[:8]}",
        config={"inbounds": [{"tag": tag} for tag in tags]},
        type=CoreType.xray,
    )
    db.add(core)
    await db.flush()
    return core.id


async def _node(db, core_id: int, status: NodeStatus = NodeStatus.connected) -> int:
    node = Node(
        name=f"cf-node-{uuid4().hex[:8]}",
        address="127.0.0.1",
        port=62050,
        api_port=62051,
        server_ca="ca",
        api_key=str(uuid4()),
        core_config_id=core_id,
        status=status,
    )
    db.add(node)
    await db.flush()
    return node.id


async def _assignment(db, node_id: int | None, inbound_tag: str, is_enabled: bool = True) -> int:
    profile = ContentFilterProfile(name=f"cf-profile-{uuid4().hex[:8]}", block_list=["example.com"], strict_mode=False)
    db.add(profile)
    await db.flush()
    assignment = ContentFilterAssignment(
        profile_id=profile.id,
        inbound_tag=inbound_tag,
        node_id=node_id,
        is_enabled=is_enabled,
    )
    db.add(assignment)
    await db.flush()
    return assignment.id


class _FakeDB:
    async def commit(self):
        return None


class _FakeGetDB:
    async def __aenter__(self):
        return _FakeDB()

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _attach_all(monkeypatch: pytest.MonkeyPatch):
    async def get_node(node_id):
        return object()

    monkeypatch.setattr(job.node_manager, "get_node", get_node)


def _nodes(*node_ids: int):
    async def listed(db):
        return list(node_ids)

    return listed


def _record_reconciles(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    seen: list[int] = []

    async def reconcile(db, node_id):
        seen.append(node_id)

    monkeypatch.setattr(service, "reconcile_node", reconcile)
    return seen


@pytest.mark.asyncio
async def test_a_node_whose_rule_vanished_has_it_pushed_back_and_is_marked_enforced(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(job, "GetDB", GetTestDB)
    _attach_all(monkeypatch)

    async with GetTestDB() as db:
        core_id = await _core(db, "in")
        node_id = await _node(db, core_id)
        assignment_id = await _assignment(db, node_id, "in")
        await db.commit()

    async with GetTestDB() as db:
        wanted = service.assignment_rules(await db.get(ContentFilterAssignment, assignment_id))

    assert wanted

    live: list[dict] = []
    pushed: list[int] = []

    async def live_rules(target_id):
        return list(live)

    async def push_live(db, target_id):
        pushed.append(target_id)
        live.extend({"ruleTag": rule["ruleTag"], "outboundTag": rule["outboundTag"]} for rule in wanted)
        return list(wanted)

    monkeypatch.setattr(service, "live_rules", live_rules)
    monkeypatch.setattr(service, "push_live", push_live)

    await job.reconcile_content_filter()

    assert pushed == [node_id]
    assert {rule["ruleTag"] for rule in live} == {rule["ruleTag"] for rule in wanted}

    async with GetTestDB() as db:
        assignment = await db.get(ContentFilterAssignment, assignment_id)
        assert assignment.enforced is True
        assert assignment.last_error is None
        assert assignment.last_checked_at is not None


@pytest.mark.asyncio
async def test_a_disabled_node_is_left_alone(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(job, "GetDB", GetTestDB)
    _attach_all(monkeypatch)
    seen = _record_reconciles(monkeypatch)

    async with GetTestDB() as db:
        core_id = await _core(db, "in")
        node_id = await _node(db, core_id, status=NodeStatus.disabled)
        await _assignment(db, node_id, "in")
        await db.commit()

    await job.reconcile_content_filter()

    assert seen == []


@pytest.mark.asyncio
async def test_a_node_without_an_enabled_assignment_is_left_alone(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(job, "GetDB", GetTestDB)
    _attach_all(monkeypatch)
    seen = _record_reconciles(monkeypatch)

    async with GetTestDB() as db:
        core_id = await _core(db, "in")
        node_id = await _node(db, core_id)
        await _assignment(db, node_id, "in", is_enabled=False)
        await db.commit()

    await job.reconcile_content_filter()

    assert seen == []


@pytest.mark.asyncio
async def test_a_fleet_wide_assignment_only_reaches_the_nodes_that_carry_its_inbound(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(job, "GetDB", GetTestDB)
    _attach_all(monkeypatch)
    seen = _record_reconciles(monkeypatch)

    async with GetTestDB() as db:
        carrying = await _node(db, await _core(db, "in"))
        await _node(db, await _core(db, "somewhere-else"))
        await _assignment(db, None, "in")
        await db.commit()

    await job.reconcile_content_filter()

    assert seen == [carrying]


@pytest.mark.asyncio
async def test_a_node_that_is_not_attached_to_this_worker_is_skipped(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(job, "GetDB", GetTestDB)
    seen = _record_reconciles(monkeypatch)

    async def get_node(node_id):
        return None

    monkeypatch.setattr(job.node_manager, "get_node", get_node)

    async with GetTestDB() as db:
        core_id = await _core(db, "in")
        node_id = await _node(db, core_id)
        await _assignment(db, node_id, "in")
        await db.commit()

    await job.reconcile_content_filter()

    assert seen == []


@pytest.mark.asyncio
async def test_one_failing_node_does_not_keep_the_others_from_being_reconciled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(job, "GetDB", _FakeGetDB)
    monkeypatch.setattr(job, "nodes_to_reconcile", _nodes(1, 2, 3))
    _attach_all(monkeypatch)
    reconciled: list[int] = []

    async def reconcile(db, node_id):
        if node_id == 2:
            raise service.EnforcementError("node 2 refuses routing rules", code=409)
        reconciled.append(node_id)

    monkeypatch.setattr(service, "reconcile_node", reconcile)

    await job.reconcile_content_filter()

    assert reconciled == [1, 3]


@pytest.mark.asyncio
async def test_a_hanging_node_is_abandoned_and_the_rest_still_run(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(job, "GetDB", _FakeGetDB)
    monkeypatch.setattr(job, "nodes_to_reconcile", _nodes(1, 2, 3))
    monkeypatch.setattr(job.job_settings, "content_filter_reconcile_timeout", 0.05)
    _attach_all(monkeypatch)
    reconciled: list[int] = []

    async def reconcile(db, node_id):
        if node_id == 2:
            await asyncio.sleep(30)
        reconciled.append(node_id)

    monkeypatch.setattr(service, "reconcile_node", reconcile)

    started = time.monotonic()
    await job.reconcile_content_filter()

    assert reconciled == [1, 3]
    assert time.monotonic() - started < 2


@pytest.mark.asyncio
async def test_queued_nodes_keep_their_whole_budget_because_the_semaphore_is_taken_first(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(job, "GetDB", _FakeGetDB)
    monkeypatch.setattr(job, "nodes_to_reconcile", _nodes(1, 2, 100, 101, 102))
    monkeypatch.setattr(job.job_settings, "content_filter_reconcile_timeout", 0.2)
    monkeypatch.setattr(job, "RECONCILE_LIMIT", 2)
    _attach_all(monkeypatch)
    reconciled: list[int] = []

    async def reconcile(db, node_id):
        if node_id < 100:
            await asyncio.sleep(30)
        await asyncio.sleep(0.05)
        reconciled.append(node_id)

    monkeypatch.setattr(service, "reconcile_node", reconcile)

    started = time.monotonic()
    await job.reconcile_content_filter()
    elapsed = time.monotonic() - started

    assert reconciled == [100, 101, 102]
    assert elapsed < 3

    async def semaphore_inside_the_timeout(node_id, sem, timeout):
        async def bounded():
            async with sem:
                await reconcile(None, node_id)

        try:
            await asyncio.wait_for(bounded(), timeout=timeout)
        except TimeoutError:
            return

    reconciled.clear()
    sem = asyncio.Semaphore(2)
    await asyncio.gather(
        *[semaphore_inside_the_timeout(node_id, sem, 0.2) for node_id in (1, 2, 100, 101, 102)],
        return_exceptions=True,
    )
    starved = list(reconciled)

    assert starved == []


@pytest.mark.asyncio
async def test_the_job_never_raises_when_the_node_lookup_fails(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(job, "GetDB", _FakeGetDB)
    _attach_all(monkeypatch)

    async def explodes(db):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(job, "nodes_to_reconcile", explodes)

    assert await job.reconcile_content_filter() is None


class _StubNodeOperation:
    @staticmethod
    async def _reconcile_extra_cores(db, node, db_node):
        return ""


def _quiet_seam(monkeypatch: pytest.MonkeyPatch):
    async def ensure_attached(node_id, node, name):
        return None

    monkeypatch.setattr(collector, "ensure_attached", ensure_attached)
    monkeypatch.setattr(node_extras, "NodeOperation", _StubNodeOperation)
    monkeypatch.setattr(node_extras, "GetDB", _FakeGetDB)


async def _drain_pending():
    while job._pending:
        for outcome in await asyncio.gather(*list(job._pending), return_exceptions=True):
            if isinstance(outcome, BaseException):
                raise outcome


@pytest.mark.asyncio
async def test_the_health_seam_reconciles_once_after_a_reconnect_and_not_on_every_tick(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(job, "GetDB", _FakeGetDB)
    _attach_all(monkeypatch)
    _quiet_seam(monkeypatch)
    seen = _record_reconciles(monkeypatch)

    db_node = SimpleNamespace(id=42, name="reconnected", last_status_change=datetime(2026, 9, 17, 10, 0, tzinfo=UTC))

    for _ in range(5):
        await node_extras.after_healthy_node_check(object(), db_node)
        await _drain_pending()

    assert seen == [42]

    db_node.last_status_change = datetime(2026, 9, 17, 10, 5, tzinfo=UTC)
    for _ in range(3):
        await node_extras.after_healthy_node_check(object(), db_node)
        await _drain_pending()

    assert seen == [42, 42]


@pytest.mark.asyncio
async def test_a_reconcile_survives_the_state_an_earlier_event_loop_left_behind(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(job, "GetDB", _FakeGetDB)
    _attach_all(monkeypatch)
    seen = _record_reconciles(monkeypatch)

    abandoned = asyncio.new_event_loop()
    abandoned.close()
    job._sem = asyncio.Semaphore(0)
    job._sem_loop = abandoned
    job._in_flight.add(11)

    job._spawn(11)
    await _drain_pending()

    assert seen == [11]
    assert job._sem_loop is asyncio.get_running_loop()
    assert 11 not in job._in_flight


@pytest.mark.asyncio
async def test_the_health_seam_still_reconciles_on_a_worker_that_is_not_the_job_leader(
    monkeypatch: pytest.MonkeyPatch,
):
    from app.nats import leader as nats_leader
    from config import nats_settings

    monkeypatch.setattr(nats_settings, "enabled", True)
    monkeypatch.setattr(nats_leader.server_settings, "workers", 2)
    monkeypatch.setattr(nats_leader, "_is_leader", False)
    assert nats_leader.needs_job_leader() and not nats_leader.is_job_leader()

    monkeypatch.setattr(job, "GetDB", _FakeGetDB)
    _attach_all(monkeypatch)
    _quiet_seam(monkeypatch)
    seen = _record_reconciles(monkeypatch)

    db_node = SimpleNamespace(id=55, name="follower", last_status_change=datetime(2026, 9, 17, 11, 0, tzinfo=UTC))
    await node_extras.after_healthy_node_check(object(), db_node)
    await _drain_pending()

    assert seen == [55]


@pytest.mark.asyncio
async def test_the_health_seam_does_nothing_while_the_off_switch_is_off(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(job, "GetDB", _FakeGetDB)
    monkeypatch.setattr(job.job_settings, "content_filter_reconcile_enabled", False)
    _attach_all(monkeypatch)
    _quiet_seam(monkeypatch)
    seen = _record_reconciles(monkeypatch)

    db_node = SimpleNamespace(id=7, name="off", last_status_change=datetime(2026, 9, 17, 10, 0, tzinfo=UTC))
    await node_extras.after_healthy_node_check(object(), db_node)
    await _drain_pending()

    assert seen == []


@pytest.mark.asyncio
async def test_the_seam_does_not_start_a_second_reconcile_while_one_is_running(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(job, "GetDB", _FakeGetDB)
    _attach_all(monkeypatch)
    _quiet_seam(monkeypatch)
    started: list[int] = []
    release = asyncio.Event()

    async def reconcile(db, node_id):
        started.append(node_id)
        await release.wait()

    monkeypatch.setattr(service, "reconcile_node", reconcile)

    db_node = SimpleNamespace(id=9, name="slow", last_status_change=datetime(2026, 9, 17, 10, 0, tzinfo=UTC))
    await node_extras.after_healthy_node_check(object(), db_node)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    monkeypatch.setattr(job, "nodes_to_reconcile", _nodes(9))
    await job.reconcile_content_filter()

    release.set()
    await _drain_pending()

    assert started == [9]


@pytest.mark.asyncio
async def test_the_off_switch_stops_the_job_from_touching_anything(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(job, "GetDB", _FakeGetDB)
    monkeypatch.setattr(job, "nodes_to_reconcile", _nodes(1, 2))
    monkeypatch.setattr(job.job_settings, "content_filter_reconcile_enabled", False)
    _attach_all(monkeypatch)
    seen = _record_reconciles(monkeypatch)

    await job.reconcile_content_filter()

    assert seen == []


@pytest.mark.asyncio
async def test_a_disabled_node_does_not_hold_a_fleet_wide_assignment_back(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(job, "GetDB", GetTestDB)
    _attach_all(monkeypatch)

    async with GetTestDB() as db:
        core_id = await _core(db, "in")
        live_node = await _node(db, core_id)
        await _node(db, core_id, status=NodeStatus.disabled)
        assignment_id = await _assignment(db, None, "in")
        await db.commit()

    async with GetTestDB() as db:
        wanted = service.assignment_rules(await db.get(ContentFilterAssignment, assignment_id))

    live = [{"ruleTag": rule["ruleTag"], "outboundTag": rule["outboundTag"]} for rule in wanted]

    async def live_rules(target_id):
        if target_id != live_node:
            raise service.EnforcementError(f"node {target_id} is not attached to this panel", code=404)
        return list(live)

    async def push_live(db, target_id):
        return list(wanted)

    monkeypatch.setattr(service, "live_rules", live_rules)
    monkeypatch.setattr(service, "push_live", push_live)

    await job.reconcile_content_filter()

    async with GetTestDB() as db:
        assignment = await db.get(ContentFilterAssignment, assignment_id)
        assert assignment.enforced is True
        assert assignment.last_error is None
