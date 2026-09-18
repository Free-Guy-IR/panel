import pytest
import pytest_asyncio
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, InvalidRequestError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.models import CoreConfig, Node, NodeStatus, node_additional_cores_association
from app.fork.content_filter import capability, rules, service
from app.fork.content_filter.schemas import (
    MAX_LIST_ENTRIES,
    AssignmentBulkPayload,
    AssignmentOutcome,
    AssignmentPayload,
    AssignmentResponse,
    ProfilePayload,
)
from app.fork.models.content_filter import ContentFilterAssignment, ContentFilterProfile
from app.fork.routers import content_filter as router
from app.models.core import CoreType


@pytest_asyncio.fixture
async def db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'content-filter-scope.sqlite3'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


def _core_config(tags: list[str], routing: bool) -> dict:
    services = ["HandlerService", "StatsService"]
    if routing:
        services.append("RoutingService")
    return {
        "api": {"tag": "API_INBOUND", "services": services},
        "inbounds": [{"tag": tag, "protocol": "vless"} for tag in tags],
    }


async def _core(db, name: str, tags: list[str], routing: bool = True) -> CoreConfig:
    core = CoreConfig(name=name, config=_core_config(tags, routing), type=CoreType.xray)
    db.add(core)
    await db.flush()
    return core


async def _node(
    db,
    name: str,
    core: CoreConfig,
    port: int = 62050,
    status: NodeStatus = NodeStatus.connected,
    node_version: str = capability.MIN_NODE_VERSION,
) -> Node:
    node = Node(
        name=name,
        address="127.0.0.1",
        port=port,
        api_port=port + 1,
        server_ca="ca",
        api_key=None,
        core_config_id=core.id,
        status=status,
    )
    node.node_version = node_version
    db.add(node)
    await db.flush()
    return node


async def _profile(db, name: str = "adult") -> ContentFilterProfile:
    profile = ContentFilterProfile(name=name, block_list=["example.com"])
    db.add(profile)
    await db.flush()
    return profile


async def _assignment(db, profile: ContentFilterProfile, tag: str, node: Node | None = None):
    assignment = ContentFilterAssignment(
        profile_id=profile.id,
        inbound_tag=tag,
        node_id=None if node is None else node.id,
        is_enabled=True,
    )
    db.add(assignment)
    await db.flush()
    return assignment


@pytest.mark.asyncio
async def test_a_filter_pinned_to_one_node_of_a_shared_core_is_delivered_live(db):
    core = await _core(db, "shared", ["in"])
    pinned_to = await _node(db, "a", core)
    await _node(db, "b", core, port=62060)
    profile = await _profile(db)
    await _assignment(db, profile, "in", node=pinned_to)
    await db.commit()

    rows = await router.list_assignments(db=db, _=None)

    assert len(rows) == 1
    assert rows[0].delivery == "live"
    assert rows[0].reaches_nodes == [pinned_to.id]


@pytest.mark.asyncio
async def test_a_filter_pinned_to_the_only_node_of_its_core_is_delivered_through_the_core(db):
    core = await _core(db, "lone", ["in"])
    node = await _node(db, "a", core)
    profile = await _profile(db)
    await _assignment(db, profile, "in", node=node)
    await db.commit()

    rows = await router.list_assignments(db=db, _=None)

    assert rows[0].delivery == "core"
    assert rows[0].reaches_nodes == [node.id]


@pytest.mark.asyncio
async def test_a_fleet_wide_filter_on_every_node_is_delivered_through_the_core(db):
    first_core = await _core(db, "a-core", ["in"])
    second_core = await _core(db, "b-core", ["in"])
    first = await _node(db, "a", first_core)
    second = await _node(db, "b", second_core, port=62060)
    profile = await _profile(db)
    await _assignment(db, profile, "in")
    await db.commit()

    rows = await router.list_assignments(db=db, _=None)

    assert rows[0].delivery == "core"
    assert rows[0].reaches_nodes == sorted([first.id, second.id])


@pytest.mark.asyncio
async def test_the_bulk_endpoint_reports_how_every_outcome_is_delivered(db, monkeypatch: pytest.MonkeyPatch):
    core = await _core(db, "shared", ["in"])
    first = await _node(db, "a", core)
    second = await _node(db, "b", core, port=62060)
    profile = await _profile(db)
    await db.commit()

    async def applied(session, assignment, admin, allow_restart=False):
        assignment.enforced = True
        return {}

    monkeypatch.setattr(service, "apply_assignment", applied)

    result = await router.create_assignments_bulk(
        payload=AssignmentBulkPayload(profile_id=profile.id, node_ids=[first.id, second.id], inbound_tags=["in"]),
        db=db,
        admin=None,
    )

    assert result.created == 2
    assert result.applied == 2
    assert [outcome.delivery for outcome in result.outcomes] == ["live", "live"]
    assert [outcome.status for outcome in result.outcomes] == ["applied", "applied"]
    assert all(outcome.enforced for outcome in result.outcomes)


@pytest.mark.asyncio
async def test_the_same_endpoint_listed_twice_is_assigned_once(db, monkeypatch: pytest.MonkeyPatch):
    core = await _core(db, "lone", ["in"])
    node = await _node(db, "a", core)
    profile = await _profile(db)
    await db.commit()

    applied_to: list[int] = []

    async def applied(session, assignment, admin, allow_restart=False):
        applied_to.append(assignment.id)
        assignment.enforced = True
        return {}

    monkeypatch.setattr(service, "apply_assignment", applied)

    result = await router.create_assignments_bulk(
        payload=AssignmentBulkPayload(profile_id=profile.id, node_ids=[node.id], inbound_tags=["in", "in"]),
        db=db,
        admin=None,
    )

    rows = (await db.execute(select(ContentFilterAssignment))).scalars().all()

    assert result.created == 1
    assert result.skipped == 0
    assert len(result.outcomes) == 1
    assert len(applied_to) == 1
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_a_node_that_refuses_the_filter_only_marks_its_own_outcome(db, monkeypatch: pytest.MonkeyPatch):
    first_core = await _core(db, "a-core", ["in"])
    second_core = await _core(db, "b-core", ["in"])
    first = await _node(db, "a", first_core)
    second = await _node(db, "b", second_core, port=62060)
    profile = await _profile(db)
    await db.commit()

    async def applied(session, assignment, admin, allow_restart=False):
        assignment.enforced = True
        if assignment.node_id == first.id:
            raise service.EnforcementError("node a refused the rule", code=409)
        return {}

    monkeypatch.setattr(service, "apply_assignment", applied)

    result = await router.create_assignments_bulk(
        payload=AssignmentBulkPayload(profile_id=profile.id, node_ids=[first.id, second.id], inbound_tags=["in"]),
        db=db,
        admin=None,
    )

    refused = next(outcome for outcome in result.outcomes if outcome.node_id == first.id)
    accepted = next(outcome for outcome in result.outcomes if outcome.node_id == second.id)

    assert refused.enforced is False
    assert refused.status == "failed"
    assert refused.detail == "node a refused the rule"
    assert refused.delivery == "core"
    assert accepted.enforced is True
    assert accepted.status == "applied"
    assert accepted.detail is None


@pytest.mark.asyncio
async def test_an_assignment_that_disappears_before_apply_does_not_break_the_batch(db, monkeypatch: pytest.MonkeyPatch):
    core = await _core(db, "lone", ["in"])
    node = await _node(db, "a", core)
    profile = await _profile(db)
    await db.commit()

    applied_to: list[int] = []

    async def applied(session, assignment, admin, allow_restart=False):
        applied_to.append(assignment.id)
        assignment.enforced = True
        return {}

    monkeypatch.setattr(service, "apply_assignment", applied)

    class _GoneOnRefresh:
        def __init__(self, session):
            self._session = session

        def __getattr__(self, name):
            return getattr(self._session, name)

        async def refresh(self, instance, *args, **kwargs):
            raise InvalidRequestError("Instance is not persistent within this Session")

    result = await router.create_assignments_bulk(
        payload=AssignmentBulkPayload(profile_id=profile.id, node_ids=[node.id], inbound_tags=["in"]),
        db=_GoneOnRefresh(db),
        admin=None,
    )

    assert result.created == 1
    assert applied_to == []
    assert result.outcomes[0].enforced is False
    assert "removed" in result.outcomes[0].detail


@pytest.mark.asyncio
async def test_targets_spell_out_where_a_pinned_filter_lands(db):
    shared = await _core(db, "shared", ["in"])
    lone = await _core(db, "lone", ["in"])
    without_routing = await _core(db, "no-routing", ["in"], routing=False)
    first = await _node(db, "a", shared)
    second = await _node(db, "b", shared, port=62060)
    alone = await _node(db, "c", lone, port=62070)
    unusable = await _node(db, "d", without_routing, port=62080)
    await db.commit()

    targets = {target.id: target for target in await router.get_targets(db=db, _=None)}

    assert targets[first.id].pinned_stays_here is True
    assert targets[first.id].would_also_affect == [second.id]
    assert "this node only" in targets[first.id].scope_note
    assert str(second.id) in targets[first.id].scope_note

    assert targets[alone.id].pinned_stays_here is True
    assert targets[alone.id].would_also_affect == []
    assert "no other node uses its core config" in targets[alone.id].scope_note

    assert targets[unusable.id].pinned_stays_here is False
    assert "reaches no node" in targets[unusable.id].scope_note


@pytest.mark.asyncio
async def test_deleting_a_profile_cleans_every_node_a_fleet_wide_filter_reached(db, monkeypatch: pytest.MonkeyPatch):
    first_core = await _core(db, "a-core", ["in"])
    second_core = await _core(db, "b-core", ["in"])
    first = await _node(db, "a", first_core)
    second = await _node(db, "b", second_core, port=62060)
    profile = await _profile(db)
    await _assignment(db, profile, "in")
    await db.commit()
    profile_id = profile.id
    db.expunge_all()

    rewritten: list[int] = []
    cleaned: list[int] = []

    async def persist(session, core_id, admin, allow_restart=False, advisories=None, drop_unsupported=False):
        rewritten.append(core_id)
        return True

    async def push(session, node_id):
        cleaned.append(node_id)
        return []

    monkeypatch.setattr(service, "persist_core_rules", persist)
    monkeypatch.setattr(service, "push_live", push)

    await router.delete_profile(profile_id, db=db, admin=None)

    assert sorted(cleaned) == sorted([first.id, second.id])
    assert sorted(rewritten) == sorted([first_core.id, second_core.id])


@pytest.mark.asyncio
async def test_a_profile_pinned_to_a_disabled_node_still_clears_that_nodes_core(db, monkeypatch: pytest.MonkeyPatch):
    core = await _core(db, "off-core", ["in"])
    node = await _node(db, "off", core)
    node.status = NodeStatus.disabled
    profile = await _profile(db)
    await _assignment(db, profile, "in", node=node)
    await db.commit()
    profile_id, core_id = profile.id, core.id
    db.expunge_all()

    rewritten: list[int] = []
    cleaned: list[int] = []

    async def persist(session, target_core, admin, allow_restart=False, advisories=None, drop_unsupported=False):
        rewritten.append(target_core)
        return True

    async def push(session, node_id):
        cleaned.append(node_id)
        return []

    monkeypatch.setattr(service, "persist_core_rules", persist)
    monkeypatch.setattr(service, "push_live", push)

    await router.delete_profile(profile_id, db=db, admin=None)

    assert rewritten == [core_id]
    assert cleaned == []


@pytest.mark.asyncio
async def test_switching_a_filter_off_in_bulk_takes_the_rules_off_the_nodes(db, monkeypatch: pytest.MonkeyPatch):
    core = await _core(db, "lone", ["in"])
    node = await _node(db, "a", core)
    profile = await _profile(db)
    assignment = await _assignment(db, profile, "in", node=node)
    assignment.enforced = True
    await db.commit()

    rewritten: list[int] = []
    cleaned: list[int] = []

    async def persist(session, target_core, admin, allow_restart=False, advisories=None, drop_unsupported=False):
        rewritten.append(target_core)
        return True

    async def push(session, node_id):
        cleaned.append(node_id)
        return []

    async def applied(session, row, admin, allow_restart=False):
        raise AssertionError("a switched-off filter must never be applied")

    monkeypatch.setattr(service, "persist_core_rules", persist)
    monkeypatch.setattr(service, "push_live", push)
    monkeypatch.setattr(service, "apply_assignment", applied)

    result = await router.create_assignments_bulk(
        payload=AssignmentBulkPayload(profile_id=profile.id, node_ids=[node.id], inbound_tags=["in"], is_enabled=False),
        db=db,
        admin=None,
    )

    row = (await db.execute(select(ContentFilterAssignment))).scalars().one()

    assert result.disabled == 1
    assert result.applied == 0
    assert result.updated == 1
    assert result.outcomes[0].status == "disabled"
    assert rewritten == [core.id]
    assert cleaned == [node.id]
    assert row.is_enabled is False
    assert row.enforced is False


@pytest.mark.asyncio
async def test_the_bulk_counts_follow_enforcement_and_not_the_database_write(db, monkeypatch: pytest.MonkeyPatch):
    good_core = await _core(db, "good-core", ["in"])
    bad_core = await _core(db, "bad-core", ["in"])
    blind_core = await _core(db, "blind-core", ["in"], routing=False)
    good = await _node(db, "good", good_core)
    bad = await _node(db, "bad", bad_core, port=62060)
    blind = await _node(db, "blind", blind_core, port=62070)
    profile = await _profile(db)
    await _assignment(db, profile, "in", node=good)
    await db.commit()

    async def applied(session, assignment, admin, allow_restart=False):
        if assignment.node_id == bad.id:
            raise service.EnforcementError("the node never answered", code=503)
        assignment.enforced = True
        return {}

    monkeypatch.setattr(service, "apply_assignment", applied)

    result = await router.create_assignments_bulk(
        payload=AssignmentBulkPayload(profile_id=profile.id, node_ids=[good.id, bad.id, blind.id], inbound_tags=["in"]),
        db=db,
        admin=None,
    )

    assert result.created == 1
    assert result.updated == 1
    assert result.applied == 1
    assert result.failed == 1
    assert result.skipped == 1
    assert result.disabled == 0


def test_a_bulk_payload_with_every_target_omitted_is_rejected():
    with pytest.raises(ValueError):
        AssignmentBulkPayload(profile_id=1)


def test_a_single_assignment_with_every_target_omitted_is_rejected():
    with pytest.raises(ValueError):
        AssignmentPayload(profile_id=1)


@pytest.mark.asyncio
async def test_the_database_refuses_a_second_fleet_wide_row_for_one_endpoint(db):
    core = await _core(db, "lone", ["in"])
    await _node(db, "a", core)
    profile = await _profile(db)
    await _assignment(db, profile, "in")
    await db.commit()

    with pytest.raises(IntegrityError):
        await _assignment(db, profile, "in")
        await db.commit()
    await db.rollback()


@pytest.mark.asyncio
async def test_duplicate_rows_left_by_an_older_database_do_not_break_the_endpoints(db, monkeypatch: pytest.MonkeyPatch):
    await db.execute(text("DROP INDEX uq_content_filter_assignments_scope_inbound"))
    core = await _core(db, "lone", ["in"])
    await _node(db, "a", core)
    profile = await _profile(db)
    await _assignment(db, profile, "in")
    await _assignment(db, profile, "in")
    await db.commit()

    async def applied(session, assignment, admin, allow_restart=False):
        assignment.enforced = True
        return {}

    monkeypatch.setattr(service, "apply_assignment", applied)

    rows = (await db.execute(select(ContentFilterAssignment))).scalars().all()
    result = await router.create_assignments_bulk(
        payload=AssignmentBulkPayload(profile_id=profile.id, inbound_tags=["in"]),
        db=db,
        admin=None,
    )

    with pytest.raises(HTTPException) as refused:
        await router.create_assignment(
            payload=AssignmentPayload(profile_id=profile.id, inbound_tag="in"),
            db=db,
            admin=None,
        )

    assert len(rows) == 2
    assert result.updated == 1
    assert refused.value.status_code == 409


@pytest.mark.asyncio
async def test_a_racing_insert_is_reported_instead_of_crashing_the_batch(db):
    core = await _core(db, "lone", ["in"])
    node = await _node(db, "a", core)
    profile = await _profile(db)
    await db.commit()

    class _Racing:
        def __init__(self, session):
            self._session = session

        def __getattr__(self, name):
            return getattr(self._session, name)

        async def commit(self):
            raise IntegrityError("INSERT", None, Exception("duplicate entry"))

    with pytest.raises(HTTPException) as refused:
        await router.create_assignments_bulk(
            payload=AssignmentBulkPayload(profile_id=profile.id, node_ids=[node.id], inbound_tags=["in"]),
            db=_Racing(db),
            admin=None,
        )

    assert refused.value.status_code == 409
    assert "try again" in refused.value.detail


@pytest.mark.asyncio
async def test_the_list_endpoint_reads_each_core_once_however_many_filters_exist(db, monkeypatch: pytest.MonkeyPatch):
    first_core = await _core(db, "a-core", ["in-1", "in-2", "in-3"])
    second_core = await _core(db, "b-core", ["in-1", "in-2", "in-3"])
    await _node(db, "a", first_core)
    await _node(db, "b", second_core, port=62060)
    profile = await _profile(db)
    for tag in ("in-1", "in-2", "in-3"):
        await _assignment(db, profile, tag)
    await db.commit()

    persisted_for: list[int] = []
    delivery_calls: list[int] = []
    real_persisted = service.core_persisted_owners
    real_delivery = service.assignment_delivery

    async def counted_persisted(session, core_id):
        persisted_for.append(core_id)
        return await real_persisted(session, core_id)

    async def counted_delivery(session, assignment):
        delivery_calls.append(assignment.id)
        return await real_delivery(session, assignment)

    monkeypatch.setattr(service, "core_persisted_owners", counted_persisted)
    monkeypatch.setattr(service, "assignment_delivery", counted_delivery)

    rows = await router.list_assignments(db=db, _=None)

    assert len(rows) == 3
    assert sorted(persisted_for) == sorted([first_core.id, second_core.id])
    assert delivery_calls == []


@pytest.mark.asyncio
async def test_the_batched_delivery_agrees_with_the_service_for_every_row(db):
    shared = await _core(db, "shared", ["in", "solo"])
    lone = await _core(db, "lone", ["in"])
    first = await _node(db, "a", shared)
    await _node(db, "b", shared, port=62060)
    alone = await _node(db, "c", lone, port=62070)
    profile = await _profile(db)
    await _assignment(db, profile, "in", node=first)
    await _assignment(db, profile, "in", node=alone)
    await _assignment(db, profile, "in")
    await _assignment(db, profile, "solo")
    await db.commit()

    rows = (await db.execute(select(ContentFilterAssignment).order_by(ContentFilterAssignment.id))).scalars().all()
    batched = await router._delivery_map(db, list(rows))
    one_by_one = {row.id: await service.assignment_delivery(db, row) for row in rows}

    assert batched == one_by_one
    assert set(batched.values()) == {"core", "live"}


@pytest.mark.asyncio
async def test_the_batched_delivery_agrees_with_the_service_for_a_profile_with_no_rules(db):
    shared = await _core(db, "shared", ["in"])
    first = await _node(db, "a", shared)
    await _node(db, "b", shared, port=62060)
    empty = ContentFilterProfile(name="empty")
    db.add(empty)
    await db.flush()
    await _assignment(db, empty, "in", node=first)
    await _assignment(db, empty, "in")
    await db.commit()

    rows = (await db.execute(select(ContentFilterAssignment).order_by(ContentFilterAssignment.id))).scalars().all()
    batched = await router._delivery_map(db, list(rows))
    one_by_one = {row.id: await service.assignment_delivery(db, row) for row in rows}

    assert service.assignment_rules(rows[0]) == []
    assert batched == one_by_one


@pytest.mark.asyncio
async def test_the_batched_delivery_agrees_with_the_service_when_a_node_runs_a_second_core(db):
    first_core = await _core(db, "a-core", ["in"])
    second_core = await _core(db, "b-core", ["in"])
    first = await _node(db, "a", first_core)
    second = await _node(db, "b", second_core, port=62060)
    await db.execute(node_additional_cores_association.insert().values(node_id=second.id, core_config_id=first_core.id))
    profile = await _profile(db)
    await _assignment(db, profile, "in", node=first)
    await _assignment(db, profile, "in")
    await db.commit()

    rows = (await db.execute(select(ContentFilterAssignment).order_by(ContentFilterAssignment.id))).scalars().all()
    batched = await router._delivery_map(db, list(rows))
    one_by_one = {row.id: await service.assignment_delivery(db, row) for row in rows}

    assert batched == one_by_one


async def _attach(db, node: Node, core: CoreConfig) -> None:
    await db.execute(node_additional_cores_association.insert().values(node_id=node.id, core_config_id=core.id))


@pytest.mark.asyncio
async def test_an_endpoint_on_an_attached_core_counts_as_the_nodes_own(db, monkeypatch: pytest.MonkeyPatch):
    plain = await _core(db, "plain", [])
    shared = await _core(db, "shared", ["in"])
    first = await _node(db, "a", shared)
    second = await _node(db, "b", plain, port=62060)
    await _attach(db, second, shared)
    profile = await _profile(db)
    await _assignment(db, profile, "in")
    await db.commit()
    profile_id = profile.id

    targets = {target.id: target for target in await router.get_targets(db=db, _=None)}
    carriers = sorted(node_id for node_id, _ in await router._carriers_of(db, "in"))
    rows = (await db.execute(select(ContentFilterAssignment).order_by(ContentFilterAssignment.id))).scalars().all()
    batched = await router._delivery_map(db, list(rows))
    one_by_one = {row.id: await service.assignment_delivery(db, row) for row in rows}

    assert [inbound.tag for inbound in targets[second.id].inbounds] == ["in"]
    assert targets[second.id].inbounds[0].filterable is True
    assert targets[second.id].reason is None
    assert targets[second.id].shares_core_with == [first.id]
    assert targets[first.id].shares_core_with == [second.id]
    assert str(first.id) in targets[second.id].scope_note
    rewritten: list[int] = []

    async def persist(session, core_id, admin, allow_restart=False, advisories=None, drop_unsupported=False):
        rewritten.append(core_id)
        return True

    async def push(session, node_id):
        return []

    monkeypatch.setattr(service, "persist_core_rules", persist)
    monkeypatch.setattr(service, "push_live", push)
    db.expunge_all()
    await router.delete_profile(profile_id, db=db, admin=None)

    assert await router._target_problem(db, second.id, "in") is None
    assert carriers == sorted([first.id, second.id])
    assert sorted(rewritten) == sorted([plain.id, shared.id])
    assert batched == one_by_one


@pytest.mark.asyncio
async def test_a_node_with_no_core_of_its_own_is_grouped_with_the_default_core(db, monkeypatch: pytest.MonkeyPatch):
    default_core = await _core(db, "default", ["in"])
    settled = await _node(db, "a", default_core)
    orphan = Node(
        name="b",
        address="127.0.0.1",
        port=62060,
        api_port=62061,
        server_ca="ca",
        api_key=None,
        core_config_id=None,
        status=NodeStatus.connected,
    )
    orphan.node_version = capability.MIN_NODE_VERSION
    db.add(orphan)
    await db.flush()
    profile = await _profile(db)
    await _assignment(db, profile, "in", node=orphan)
    await db.commit()
    profile_id = profile.id

    targets = {target.id: target for target in await router.get_targets(db=db, _=None)}
    rows = (await db.execute(select(ContentFilterAssignment).order_by(ContentFilterAssignment.id))).scalars().all()
    batched = await router._delivery_map(db, list(rows))
    one_by_one = {row.id: await service.assignment_delivery(db, row) for row in rows}

    assert default_core.id == service.DEFAULT_CORE_ID
    assert targets[orphan.id].core_config_id == service.DEFAULT_CORE_ID
    rewritten: list[int] = []

    async def persist(session, core_id, admin, allow_restart=False, advisories=None, drop_unsupported=False):
        rewritten.append(core_id)
        return True

    async def push(session, node_id):
        return []

    monkeypatch.setattr(service, "persist_core_rules", persist)
    monkeypatch.setattr(service, "push_live", push)
    db.expunge_all()
    await router.delete_profile(profile_id, db=db, admin=None)

    assert targets[orphan.id].shares_core_with == [settled.id]
    assert [inbound.tag for inbound in targets[orphan.id].inbounds] == ["in"]
    assert rewritten == [service.DEFAULT_CORE_ID]
    assert batched == one_by_one


@pytest.mark.parametrize(
    "entry",
    [
        "geosite:category-ads",
        "regexp:.*ads.*",
        "keyword:ads",
        "=regexp:.*",
        "ex,ample.com",
        "exa mple.com",
        "ads(1).com",
        "site`.com",
        "bad\x00.com",
        "bell\x07.com",
        "a..b.com",
        ".example.com",
        "x" * 64 + ".example.com",
        "*example.com",
        "foo=bar.com",
        "sub.*.example.com",
        "*",
        "*.",
        "=",
        "...",
    ],
)
def test_a_domain_that_could_never_reach_the_config_is_refused(entry):
    with pytest.raises(ValueError) as refused:
        ProfilePayload(name="p", block_list=[entry])

    assert "cannot be used as a domain" in str(refused.value)


def test_only_blank_entries_are_still_dropped_in_silence():
    payload = ProfilePayload(name="p", block_list=["", "   ", "example.com"])

    assert payload.block_list == ["example.com"]


def test_an_over_long_list_is_refused_rather_than_trimmed():
    with pytest.raises(ValueError) as refused:
        ProfilePayload(name="p", block_list=[f"host{index}.example.com" for index in range(MAX_LIST_ENTRIES + 1)])

    assert "or fewer" in str(refused.value)


def test_international_domains_still_pass():
    payload = ProfilePayload(name="p", block_list=["نمونه.ایران", "*.例え.テスト", "=Пример.РФ", "Example.COM."])

    assert payload.block_list == ["نمونه.ایران", "*.例え.テスト", "=пример.рф", "example.com"]


def test_every_domain_the_validator_accepts_can_be_turned_into_a_rule():
    payload = ProfilePayload(
        name="p",
        allow_list=["=exact.example.com"],
        block_list=["example.com", "*.sub.example.com", "نمونه.ایران", "under_score.example.com"],
    )

    built = rules.build_rules(
        assignment_id=1,
        inbound_tags=["in"],
        categories=[],
        allow_list=payload.allow_list,
        block_list=payload.block_list,
        strict_mode=False,
    )

    assert len(built) == 2


@pytest.mark.asyncio
async def test_a_profile_saved_before_these_rules_leaves_every_other_row_readable(db):
    core = await _core(db, "lone", ["in", "other"])
    node = await _node(db, "a", core)
    healthy = await _profile(db)
    legacy = ContentFilterProfile(name="legacy", block_list=["geosite:category-ads"])
    db.add(legacy)
    await db.flush()
    good = await _assignment(db, healthy, "other", node=node)
    poisoned = await _assignment(db, legacy, "in", node=node)
    await db.commit()

    rows = {row.id: row for row in await router.list_assignments(db=db, _=None)}

    assert sorted(rows) == sorted([good.id, poisoned.id])
    assert rows[good.id].delivery == "core"
    assert rows[good.id].reaches_nodes == [node.id]
    assert rows[poisoned.id].enforced is False
    assert rows[poisoned.id].delivery == await service.assignment_delivery(db, poisoned)


CLASH = (
    "a rule already on this node is evaluated before the filter and could carry the same traffic: "
    "pgcf-free-routing. Remove that rule, or narrow it to the endpoints the filter does not cover, "
    "then apply the filter again."
)


@pytest.mark.asyncio
async def test_a_clashing_rule_is_reported_word_for_word_on_every_bulk_outcome(db, monkeypatch: pytest.MonkeyPatch):
    core = await _core(db, "shared", ["in"])
    first = await _node(db, "a", core)
    second = await _node(db, "b", core, port=62060)
    profile = await _profile(db)
    await db.commit()

    async def refuses(session, assignment, admin, allow_restart=False):
        raise service.EnforcementError(CLASH, code=409)

    monkeypatch.setattr(service, "apply_assignment", refuses)

    result = await router.create_assignments_bulk(
        payload=AssignmentBulkPayload(profile_id=profile.id, node_ids=[first.id, second.id], inbound_tags=["in"]),
        db=db,
        admin=None,
    )

    assert [outcome.detail for outcome in result.outcomes] == [CLASH, CLASH]
    assert [outcome.status for outcome in result.outcomes] == ["failed", "failed"]
    assert result.applied == 0
    assert result.failed == 2


@pytest.mark.asyncio
async def test_a_clashing_rule_keeps_its_status_and_wording_on_the_single_endpoints(
    db, monkeypatch: pytest.MonkeyPatch
):
    core = await _core(db, "lone", ["in"])
    node = await _node(db, "a", core)
    profile = await _profile(db)
    assignment = await _assignment(db, profile, "in", node=node)
    await db.commit()

    async def refuses(session, row, admin, allow_restart=False):
        raise service.EnforcementError(CLASH, code=409)

    monkeypatch.setattr(service, "apply_assignment", refuses)

    with pytest.raises(HTTPException) as applied:
        await router.apply_assignment(assignment.id, db=db, admin=None)

    with pytest.raises(HTTPException) as created:
        await router.create_assignment(
            payload=AssignmentPayload(profile_id=profile.id, node_id=node.id, inbound_tag="in", is_enabled=True),
            db=db,
            admin=None,
        )

    assert applied.value.status_code == 409
    assert applied.value.detail == CLASH
    assert created.value.status_code == 409


@pytest.mark.asyncio
async def test_a_long_clash_report_is_not_truncated_on_its_way_to_the_operator(db, monkeypatch: pytest.MonkeyPatch):
    core = await _core(db, "lone", ["in"])
    node = await _node(db, "a", core)
    profile = await _profile(db)
    await db.commit()
    verbose = CLASH + " " + "x" * 1400

    async def refuses(session, assignment, admin, allow_restart=False):
        raise service.EnforcementError(verbose, code=409)

    monkeypatch.setattr(service, "apply_assignment", refuses)

    result = await router.create_assignments_bulk(
        payload=AssignmentBulkPayload(profile_id=profile.id, node_ids=[node.id], inbound_tags=["in"]),
        db=db,
        admin=None,
    )

    assert result.outcomes[0].detail == verbose


@pytest.mark.asyncio
async def test_a_clash_while_switching_a_filter_off_is_reported_in_full(db, monkeypatch: pytest.MonkeyPatch):
    core = await _core(db, "lone", ["in"])
    node = await _node(db, "a", core)
    profile = await _profile(db)
    assignment = await _assignment(db, profile, "in", node=node)
    assignment.enforced = True
    await db.commit()
    verbose = CLASH + " " + "y" * 1400

    async def persist(session, core_id, admin, allow_restart=False, advisories=None, drop_unsupported=False):
        return True

    async def refuses(session, node_id):
        raise service.EnforcementError(verbose, code=409)

    monkeypatch.setattr(service, "persist_core_rules", persist)
    monkeypatch.setattr(service, "push_live", refuses)

    result = await router.create_assignments_bulk(
        payload=AssignmentBulkPayload(profile_id=profile.id, node_ids=[node.id], inbound_tags=["in"], is_enabled=False),
        db=db,
        admin=None,
    )

    row = (await db.execute(select(ContentFilterAssignment))).scalars().one()

    assert result.outcomes[0].status == "disabled"
    assert result.outcomes[0].detail.endswith(verbose)
    assert len(row.last_error) == 1024


ADVISORIES = (
    "core 1: a rule that applies to every inbound could send a hostname matching keyword:instagram to DIRECT",
    "node 7: a rule that applies to every inbound could send a hostname matching keyword:fbcdn to DIRECT",
)


@pytest.mark.asyncio
async def test_an_orm_row_cannot_be_turned_into_a_response_behind_the_views_back(db):
    core = await _core(db, "lone", ["in"])
    node = await _node(db, "a", core)
    profile = await _profile(db)
    row = await _assignment(db, profile, "in", node=node)
    await db.commit()

    with pytest.raises(ValidationError):
        AssignmentResponse.model_validate(row)

    view = await router._assignment_view(db, row)

    assert view.id == row.id
    assert view.delivery == "core"
    assert view.reaches_nodes == [node.id]
    assert view.advisories == []
    assert view.advisory_note == ""


@pytest.mark.asyncio
async def test_an_apply_with_advisories_reports_success_and_repeats_them_word_for_word(
    db, monkeypatch: pytest.MonkeyPatch
):
    core = await _core(db, "lone", ["in"])
    node = await _node(db, "a", core)
    profile = await _profile(db)
    assignment = await _assignment(db, profile, "in", node=node)
    await db.commit()

    async def applied(session, row, admin, allow_restart=False):
        row.enforced = True
        return {
            "rules": 2,
            "nodes": 1,
            "probe": "BLOCK",
            "advisories": list(ADVISORIES),
            "advisory_note": rules.ADVISORY_NOTE,
        }

    monkeypatch.setattr(service, "apply_assignment", applied)

    view = await router.apply_assignment(assignment.id, db=db, admin=None)

    assert view.enforced is True
    assert view.advisories == list(ADVISORIES)
    assert view.advisory_note == rules.ADVISORY_NOTE


@pytest.mark.asyncio
async def test_a_new_assignment_carries_its_advisories_back_to_the_operator(db, monkeypatch: pytest.MonkeyPatch):
    core = await _core(db, "lone", ["in"])
    node = await _node(db, "a", core)
    profile = await _profile(db)
    await db.commit()

    async def applied(session, row, admin, allow_restart=False):
        row.enforced = True
        return {
            "rules": 1,
            "nodes": 1,
            "probe": "BLOCK",
            "advisories": [ADVISORIES[0]],
            "advisory_note": rules.ADVISORY_NOTE,
        }

    monkeypatch.setattr(service, "apply_assignment", applied)

    view = await router.create_assignment(
        payload=AssignmentPayload(profile_id=profile.id, node_id=node.id, inbound_tag="in"),
        db=db,
        admin=None,
    )

    assert view.enforced is True
    assert view.advisories == [ADVISORIES[0]]
    assert view.advisory_note == rules.ADVISORY_NOTE


@pytest.mark.asyncio
async def test_a_bulk_run_carries_the_advisories_on_every_outcome(db, monkeypatch: pytest.MonkeyPatch):
    core = await _core(db, "shared", ["in"])
    first = await _node(db, "a", core)
    second = await _node(db, "b", core, port=62060)
    profile = await _profile(db)
    await db.commit()

    async def applied(session, row, admin, allow_restart=False):
        row.enforced = True
        return {
            "rules": 2,
            "nodes": 1,
            "probe": "BLOCK",
            "advisories": list(ADVISORIES),
            "advisory_note": rules.ADVISORY_NOTE,
        }

    monkeypatch.setattr(service, "apply_assignment", applied)

    result = await router.create_assignments_bulk(
        payload=AssignmentBulkPayload(profile_id=profile.id, node_ids=[first.id, second.id], inbound_tags=["in"]),
        db=db,
        admin=None,
    )

    assert result.applied == 2
    assert result.failed == 0
    assert [outcome.status for outcome in result.outcomes] == ["applied", "applied"]
    assert all(outcome.enforced for outcome in result.outcomes)
    assert [outcome.advisories for outcome in result.outcomes] == [list(ADVISORIES), list(ADVISORIES)]
    assert all(outcome.advisory_note == rules.ADVISORY_NOTE for outcome in result.outcomes)


@pytest.mark.asyncio
async def test_no_advisories_reads_as_an_empty_list_and_an_empty_note(db, monkeypatch: pytest.MonkeyPatch):
    core = await _core(db, "lone", ["in"])
    node = await _node(db, "a", core)
    profile = await _profile(db)
    assignment = await _assignment(db, profile, "in", node=node)
    await db.commit()

    async def applied(session, row, admin, allow_restart=False):
        row.enforced = True
        return {"rules": 1, "nodes": 1, "probe": "BLOCK", "advisories": [], "advisory_note": ""}

    monkeypatch.setattr(service, "apply_assignment", applied)

    view = await router.apply_assignment(assignment.id, db=db, admin=None)
    listed = await router.list_assignments(db=db, _=None)
    result = await router.create_assignments_bulk(
        payload=AssignmentBulkPayload(profile_id=profile.id, node_ids=[node.id], inbound_tags=["in"]),
        db=db,
        admin=None,
    )

    assert view.model_dump()["advisories"] == []
    assert view.model_dump()["advisory_note"] == ""
    assert listed[0].advisories == []
    assert listed[0].advisory_note == ""
    assert result.outcomes[0].advisories == []
    assert result.outcomes[0].advisory_note == ""


def _response_fields() -> dict:
    return {
        "id": 1,
        "profile_id": 1,
        "node_id": None,
        "inbound_tag": "in",
        "is_enabled": True,
        "enforced": True,
        "last_checked_at": None,
        "last_error": None,
        "delivery": "core",
        "reaches_nodes": [1, 2],
    }


def test_neither_derived_answer_may_be_left_out_of_a_response():
    complete = _response_fields()

    assert AssignmentResponse(**complete).reaches_nodes == [1, 2]

    for derived in ("delivery", "reaches_nodes"):
        with pytest.raises(ValidationError):
            AssignmentResponse(**{key: value for key, value in complete.items() if key != derived})


def test_a_response_without_an_apply_behind_it_still_has_notice_fields_to_render():
    view = AssignmentResponse(**_response_fields())
    untouched = AssignmentOutcome(node_id=4, inbound_tag="in", created=False, status="skipped", detail="no")

    assert view.advisories == []
    assert view.advisory_note == ""
    assert untouched.advisories == []
    assert untouched.advisory_note == ""
    assert AssignmentResponse(**_response_fields()).advisories is not view.advisories


RELOAD_NODES = [7, 9]
RELOAD_TAGS = ["in", "in-2"]
RELOAD_MESSAGE = (
    "core 1 needs name recovery changed on ['in', 'in-2'], which only takes effect after a reload: "
    "nodes [7, 9] would restart and drop their current sessions"
)


def _gated_apply(applied_to: list[int]):
    async def gated(session, row, admin, allow_restart=False):
        if not allow_restart:
            raise service.ReloadRequired(RELOAD_MESSAGE, node_ids=list(RELOAD_NODES), inbound_tags=list(RELOAD_TAGS))
        applied_to.append(row.id)
        row.enforced = True
        return {"rules": 1, "nodes": 1, "probe": "BLOCK", "advisories": [], "advisory_note": ""}

    return gated


def _assert_prompt(detail: dict) -> None:
    assert detail["reason"] == "reload_required"
    assert detail["message"] == RELOAD_MESSAGE
    assert detail["node_ids"] == RELOAD_NODES
    assert detail["inbound_tags"] == RELOAD_TAGS
    assert detail["confirm_with"] == "confirm_restart"


@pytest.mark.asyncio
async def test_an_apply_that_would_restart_nodes_asks_first_and_names_them(db, monkeypatch: pytest.MonkeyPatch):
    core = await _core(db, "lone", ["in"])
    node = await _node(db, "a", core)
    profile = await _profile(db)
    assignment = await _assignment(db, profile, "in", node=node)
    await db.commit()
    applied_to: list[int] = []
    monkeypatch.setattr(service, "apply_assignment", _gated_apply(applied_to))

    with pytest.raises(HTTPException) as asked:
        await router.apply_assignment(assignment.id, db=db, admin=None)

    assert asked.value.status_code == 409
    _assert_prompt(asked.value.detail)
    assert applied_to == []


@pytest.mark.asyncio
async def test_a_confirmed_apply_goes_ahead_with_the_restart(db, monkeypatch: pytest.MonkeyPatch):
    core = await _core(db, "lone", ["in"])
    node = await _node(db, "a", core)
    profile = await _profile(db)
    assignment = await _assignment(db, profile, "in", node=node)
    await db.commit()
    applied_to: list[int] = []
    monkeypatch.setattr(service, "apply_assignment", _gated_apply(applied_to))

    view = await router.apply_assignment(assignment.id, confirm_restart=True, db=db, admin=None)

    assert applied_to == [assignment.id]
    assert view.enforced is True


@pytest.mark.asyncio
async def test_a_new_assignment_that_would_restart_nodes_asks_first(db, monkeypatch: pytest.MonkeyPatch):
    core = await _core(db, "lone", ["in", "in-2"])
    node = await _node(db, "a", core)
    profile = await _profile(db)
    await db.commit()
    applied_to: list[int] = []
    monkeypatch.setattr(service, "apply_assignment", _gated_apply(applied_to))

    with pytest.raises(HTTPException) as asked:
        await router.create_assignment(
            payload=AssignmentPayload(profile_id=profile.id, node_id=node.id, inbound_tag="in"),
            db=db,
            admin=None,
        )

    assert asked.value.status_code == 409
    _assert_prompt(asked.value.detail)

    view = await router.create_assignment(
        payload=AssignmentPayload(profile_id=profile.id, node_id=node.id, inbound_tag="in-2", confirm_restart=True),
        db=db,
        admin=None,
    )

    assert view.enforced is True


@pytest.mark.asyncio
async def test_a_withdrawal_that_would_restart_nodes_asks_first(db, monkeypatch: pytest.MonkeyPatch):
    core = await _core(db, "lone", ["in"])
    node = await _node(db, "a", core)
    profile = await _profile(db)
    assignment = await _assignment(db, profile, "in", node=node)
    await db.commit()
    removed: list[int] = []

    async def gated(session, row, admin, allow_restart=False):
        if not allow_restart:
            raise service.ReloadRequired(RELOAD_MESSAGE, node_ids=list(RELOAD_NODES), inbound_tags=list(RELOAD_TAGS))
        removed.append(row.id)

    monkeypatch.setattr(service, "withdraw_assignment", gated)

    with pytest.raises(HTTPException) as asked:
        await router.delete_assignment(assignment.id, db=db, admin=None)

    assert asked.value.status_code == 409
    _assert_prompt(asked.value.detail)
    assert removed == []


@pytest.mark.asyncio
async def test_a_confirmed_withdrawal_goes_ahead_with_the_restart(db, monkeypatch: pytest.MonkeyPatch):
    core = await _core(db, "lone", ["in"])
    node = await _node(db, "a", core)
    profile = await _profile(db)
    assignment = await _assignment(db, profile, "in", node=node)
    await db.commit()
    removed: list[int] = []

    async def gated(session, row, admin, allow_restart=False):
        if not allow_restart:
            raise service.ReloadRequired(RELOAD_MESSAGE, node_ids=list(RELOAD_NODES), inbound_tags=list(RELOAD_TAGS))
        removed.append(row.id)

    monkeypatch.setattr(service, "withdraw_assignment", gated)

    await router.delete_assignment(assignment.id, confirm_restart=True, db=db, admin=None)

    assert removed == [assignment.id]


@pytest.mark.asyncio
async def test_a_bulk_run_hands_the_restart_prompt_to_the_outcome_that_needs_it(db, monkeypatch: pytest.MonkeyPatch):
    core = await _core(db, "shared", ["in"])
    first = await _node(db, "a", core)
    second = await _node(db, "b", core, port=62060)
    profile = await _profile(db)
    await db.commit()
    applied_to: list[int] = []
    monkeypatch.setattr(service, "apply_assignment", _gated_apply(applied_to))

    refused = await router.create_assignments_bulk(
        payload=AssignmentBulkPayload(profile_id=profile.id, node_ids=[first.id, second.id], inbound_tags=["in"]),
        db=db,
        admin=None,
    )
    confirmed = await router.create_assignments_bulk(
        payload=AssignmentBulkPayload(
            profile_id=profile.id, node_ids=[first.id, second.id], inbound_tags=["in"], confirm_restart=True
        ),
        db=db,
        admin=None,
    )

    assert refused.applied == 0
    assert refused.failed == 2
    assert [outcome.status for outcome in refused.outcomes] == ["failed", "failed"]
    assert [outcome.detail for outcome in refused.outcomes] == [RELOAD_MESSAGE, RELOAD_MESSAGE]
    for outcome in refused.outcomes:
        assert outcome.reload is not None
        _assert_prompt(outcome.reload.model_dump())
    assert confirmed.applied == 2
    assert confirmed.updated == 2
    assert all(outcome.reload is None for outcome in confirmed.outcomes)
    assert len(applied_to) == 2
