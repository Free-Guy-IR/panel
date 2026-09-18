import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.crud.core import get_core_config_by_id
from app.db.models import CoreConfig, Node, NodeStatus
from app.fork.content_filter import capability, rules, service
from app.fork.content_filter.schemas import AssignmentBulkPayload, NodeCapability
from app.fork.models.content_filter import ContentFilterAssignment, ContentFilterProfile
from app.fork.routers import content_filter as router
from app.models.core import CoreType

CURRENT = capability.MIN_NODE_VERSION
OUTDATED = "0.6.17"

FILTER_OUTBOUNDS = [
    {"tag": "BLOCK", "protocol": "blackhole"},
    {"tag": "DIRECT", "protocol": "freedom"},
    {"tag": "wg-out", "protocol": "wireguard"},
]


@pytest_asyncio.fixture
async def db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'content-filter-capability.sqlite3'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


def _config(tags: list[str], routing_rules: list[dict] | None = None) -> dict:
    return {
        "api": {"tag": "API_INBOUND", "services": ["HandlerService", "StatsService", "RoutingService"]},
        "inbounds": [{"tag": tag, "protocol": "vless"} for tag in tags],
        "outbounds": list(FILTER_OUTBOUNDS),
        "routing": {"domainStrategy": "IPIfNonMatch", "rules": list(routing_rules or [])},
    }


async def _core(
    db,
    name: str,
    tags: list[str],
    core_type: CoreType = CoreType.xray,
    routing_rules: list[dict] | None = None,
) -> CoreConfig:
    core = CoreConfig(name=name, config=_config(tags, routing_rules), type=core_type)
    db.add(core)
    await db.flush()
    return core


async def _node(
    db,
    name: str,
    core: CoreConfig,
    port: int,
    status: NodeStatus = NodeStatus.connected,
    node_version: str = CURRENT,
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


async def _profile(db, name: str = "adult", strict_mode: bool = False) -> ContentFilterProfile:
    profile = ContentFilterProfile(name=name, block_list=["example.com"], strict_mode=strict_mode)
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


def _core_writer(monkeypatch, captured: dict) -> None:
    import app.operation.core as core_operation

    class _Operation:
        def __init__(self, operator_type=None):
            self.operator_type = operator_type

        async def modify_core(self, session, core_id, payload, admin):
            captured.setdefault("cores", []).append(core_id)
            captured["config"] = payload.config

    class _Manager:
        async def get_node(self, node_id):
            return None

    monkeypatch.setattr(core_operation, "CoreOperation", _Operation)
    monkeypatch.setattr(service, "node_manager", _Manager())


def _owned(config: dict) -> list[str]:
    stored = (config.get("routing") or {}).get("rules") or []
    return [str(rule.get("ruleTag") or "") for rule in stored if rules.owns_tag(str(rule.get("ruleTag") or ""))]


def _pass_through(inbound_tag: str) -> dict:
    return {"type": "field", "inboundTag": [inbound_tag], "outboundTag": "wg-out"}


@pytest.mark.asyncio
async def test_a_rule_is_not_written_into_a_core_an_attached_node_cannot_resolve(db, monkeypatch):
    core = await _core(db, "shared", ["in"])
    await _node(db, "fresh", core, 62050)
    stale = await _node(db, "stale", core, 62060, node_version=OUTDATED)
    profile = await _profile(db)
    await _assignment(db, profile, "in")
    await db.commit()

    written: dict = {}
    _core_writer(monkeypatch, written)

    with pytest.raises(service.UnsupportedNodes) as refused:
        await service.persist_core_rules(db, core.id, admin=None, allow_restart=True)

    assert refused.value.code == 409
    assert [entry.id for entry in refused.value.blockers] == [stale.id]
    assert refused.value.blockers[0].reason == capability.NODE_OUTDATED
    assert "stale" in refused.value.detail
    assert written == {}

    stored = await get_core_config_by_id(db, core.id)
    assert _owned(stored.config) == []


@pytest.mark.asyncio
async def test_a_core_whose_nodes_all_carry_the_assets_is_written(db, monkeypatch):
    core = await _core(db, "shared", ["in"])
    await _node(db, "fresh", core, 62050)
    await _node(db, "also-fresh", core, 62060)
    profile = await _profile(db)
    await _assignment(db, profile, "in")
    await db.commit()

    written: dict = {}
    _core_writer(monkeypatch, written)

    assert await service.persist_core_rules(db, core.id, admin=None, allow_restart=True) is True
    assert len(_owned(written["config"])) == 1


@pytest.mark.asyncio
async def test_a_disconnected_node_on_the_core_also_refuses_the_write(db, monkeypatch):
    core = await _core(db, "shared", ["in"])
    await _node(db, "fresh", core, 62050)
    flapping = await _node(db, "flapping", core, 62060, status=NodeStatus.connecting)
    profile = await _profile(db)
    await _assignment(db, profile, "in")
    await db.commit()

    written: dict = {}
    _core_writer(monkeypatch, written)

    with pytest.raises(service.UnsupportedNodes) as refused:
        await service.persist_core_rules(db, core.id, admin=None, allow_restart=True)

    assert [entry.reason for entry in refused.value.blockers] == [capability.NODE_DISCONNECTED]
    assert [entry.id for entry in refused.value.blockers] == [flapping.id]
    assert written == {}


@pytest.mark.asyncio
async def test_taking_a_filter_off_a_core_still_works_when_a_node_is_outdated(db, monkeypatch):
    core = await _core(db, "shared", ["in"])
    await _node(db, "fresh", core, 62050)
    await _node(db, "stale", core, 62060, node_version=OUTDATED)
    profile = await _profile(db)
    assignment = await _assignment(db, profile, "in")
    await db.flush()
    stale_tag = service.assignment_rules(assignment, ["in"])[0]["ruleTag"]
    config = dict(core.config)
    config["routing"] = {**config["routing"], "rules": [{"ruleTag": stale_tag, "outboundTag": "BLOCK"}]}
    core.config = config
    assignment.is_enabled = False
    await db.commit()

    written: dict = {}
    _core_writer(monkeypatch, written)

    await service.persist_core_rules(db, core.id, admin=None, allow_restart=True, drop_unsupported=True)

    assert _owned(written["config"]) == []


@pytest.mark.asyncio
async def test_an_empty_profile_is_still_refused_before_the_capability_check(db, monkeypatch):
    core = await _core(db, "shared", ["in"])
    await _node(db, "stale", core, 62050, node_version=OUTDATED)
    profile = ContentFilterProfile(name="empty", strict_mode=False)
    db.add(profile)
    await db.flush()
    assignment = await _assignment(db, profile, "in")
    await db.commit()

    written: dict = {}
    _core_writer(monkeypatch, written)

    with pytest.raises(service.EnforcementError) as refused:
        await service.apply_assignment(db, assignment, admin=None, allow_restart=True)

    assert refused.value.code == 409
    assert "nothing to apply" in refused.value.detail
    assert written == {}


@pytest.mark.asyncio
async def test_a_profile_that_blocks_everything_is_still_refused(db, monkeypatch):
    core = await _core(db, "shared", ["in"])
    await _node(db, "fresh", core, 62050)
    profile = await _profile(db, name="strict", strict_mode=True)
    assignment = await _assignment(db, profile, "in")
    await db.commit()

    written: dict = {}
    _core_writer(monkeypatch, written)

    with pytest.raises(service.EnforcementError) as refused:
        await service.apply_assignment(db, assignment, admin=None, allow_restart=True)

    assert refused.value.code == 409
    assert "strict mode" in refused.value.detail
    assert written == {}


async def _mixed_fleet(db):
    main = await _core(db, "main", ["spof 401", "india"], routing_rules=[_pass_through("india")])
    singbox = await _core(db, "singbox", ["spof 401", "sb-in"], core_type=CoreType.singbox)
    old = await _core(db, "old", ["old-in"])
    broken = await _core(db, "broken", ["err-in"])
    await _core(db, "spare", ["orphan-in"])

    first = await _node(db, "first", main, 62050)
    second = await _node(db, "second", main, 62060)
    sing = await _node(db, "sing", singbox, 62070)
    stale = await _node(db, "stale", old, 62080, node_version=OUTDATED)
    down = await _node(db, "down", broken, 62090, status=NodeStatus.error)
    await db.commit()
    return {
        "first": first,
        "second": second,
        "sing": sing,
        "stale": stale,
        "down": down,
        "main": main,
        "singbox": singbox,
    }


@pytest.mark.asyncio
async def test_the_capability_endpoint_names_every_node_and_why(db):
    fleet = await _mixed_fleet(db)

    report = await router.get_capability(db=db, _=None)
    by_id = {entry.id: entry for entry in report.nodes}

    assert by_id[fleet["first"].id].supported is True
    assert by_id[fleet["first"].id].reason is None
    assert by_id[fleet["first"].id].core_type == "xray"
    assert by_id[fleet["first"].id].node_version == CURRENT
    assert by_id[fleet["first"].id].core_config_id == fleet["main"].id

    assert by_id[fleet["sing"].id].reason == capability.CORE_NOT_XRAY
    assert by_id[fleet["sing"].id].core_type == "singbox"
    assert by_id[fleet["stale"].id].reason == capability.NODE_OUTDATED
    assert by_id[fleet["down"].id].reason == capability.NODE_DISCONNECTED
    assert by_id[fleet["down"].id].status == "error"
    assert all(entry.supported is False for entry in (by_id[fleet["sing"].id], by_id[fleet["stale"].id]))


@pytest.mark.asyncio
async def test_the_capability_endpoint_names_every_endpoint_and_why(db):
    fleet = await _mixed_fleet(db)

    report = await router.get_capability(db=db, _=None)
    by_tag = {entry.tag: entry for entry in report.inbound_tags}

    assert "API_INBOUND" not in by_tag

    shared = by_tag["spof 401"]
    assert shared.supported is True
    assert shared.reason is None
    assert shared.supported_node_ids == sorted([fleet["first"].id, fleet["second"].id])
    assert shared.unsupported_node_ids == [fleet["sing"].id]

    assert by_tag["india"].supported is False
    assert by_tag["india"].reason == capability.PRE_ROUTED
    assert by_tag["india"].supported_node_ids == []
    assert by_tag["india"].unsupported_node_ids == sorted([fleet["first"].id, fleet["second"].id])

    assert by_tag["sb-in"].reason == capability.CORE_NOT_XRAY
    assert by_tag["old-in"].reason == capability.NODE_OUTDATED
    assert by_tag["err-in"].reason == capability.NODE_DISCONNECTED

    orphan = by_tag["orphan-in"]
    assert orphan.reason == capability.NO_NODES
    assert orphan.supported is False
    assert orphan.supported_node_ids == []
    assert orphan.unsupported_node_ids == []


@pytest.mark.asyncio
async def test_the_capability_response_keeps_the_shape_the_dashboard_reads(db):
    await _mixed_fleet(db)

    payload = (await router.get_capability(db=db, _=None)).model_dump()

    assert set(payload) == {"nodes", "inbound_tags"}
    assert set(payload["nodes"][0]) == {
        "id",
        "name",
        "core_config_id",
        "core_type",
        "node_version",
        "status",
        "supported",
        "reason",
    }
    assert set(payload["inbound_tags"][0]) == {
        "tag",
        "supported",
        "supported_node_ids",
        "unsupported_node_ids",
        "reason",
    }
    reported = {entry["reason"] for entry in payload["nodes"] + payload["inbound_tags"]}
    assert reported <= {None, *capability.REASONS}
    assert reported >= set(capability.REASONS)


@pytest.mark.asyncio
async def test_a_rule_that_narrows_its_traffic_does_not_count_as_pre_routing():
    assert rules.pre_routed_inbound_tags(_config(["in"], [_pass_through("in")])) == {"in"}
    narrowed = {"type": "field", "inboundTag": ["in"], "domain": ["domain:example.com"], "outboundTag": "wg-out"}
    assert rules.pre_routed_inbound_tags(_config(["in"], [narrowed])) == set()
    by_port = {"type": "field", "inboundTag": ["in"], "port": "443", "outboundTag": "wg-out"}
    assert rules.pre_routed_inbound_tags(_config(["in"], [by_port])) == set()


@pytest.mark.asyncio
async def test_a_rule_with_no_inbound_of_its_own_pre_routes_every_endpoint():
    catch_all = {"type": "field", "outboundTag": "wg-out"}
    assert rules.pre_routed_inbound_tags(_config(["a", "b"], [catch_all])) == {"a", "b"}


@pytest.mark.asyncio
async def test_the_filters_own_rules_never_count_as_pre_routing():
    owned = {"type": "field", "ruleTag": rules.rule_tag(7, "block", "abcdef0123"), "outboundTag": "BLOCK"}
    assert rules.pre_routed_inbound_tags(_config(["a"], [owned])) == set()


def _stub_apply(monkeypatch, applied: list):
    async def apply(session, assignment, admin, allow_restart=False):
        applied.append(assignment.id)
        assignment.enforced = True
        return {}

    monkeypatch.setattr(service, "apply_assignment", apply)


@pytest.mark.asyncio
async def test_a_bulk_run_skips_the_node_whose_image_is_too_old(db, monkeypatch):
    fresh_core = await _core(db, "fresh-core", ["in-fresh"])
    stale_core = await _core(db, "stale-core", ["in-stale"])
    fresh = await _node(db, "fresh", fresh_core, 62050)
    stale = await _node(db, "stale", stale_core, 62060, node_version=OUTDATED)
    profile = await _profile(db)
    await db.commit()

    applied: list[int] = []
    _stub_apply(monkeypatch, applied)

    result = await router.create_assignments_bulk(
        AssignmentBulkPayload(profile_id=profile.id, node_ids=[fresh.id, stale.id]),
        db=db,
        admin=None,
    )

    skipped = [outcome for outcome in result.outcomes if outcome.status == "skipped"]
    assert [outcome.node_id for outcome in skipped] == [stale.id]
    assert [outcome.reason for outcome in skipped] == [capability.NODE_OUTDATED]
    assert result.skipped == 1
    assert result.applied == 1
    assert len(applied) == 1

    rows = (await db.execute(ContentFilterAssignment.__table__.select())).all()
    assert [row.node_id for row in rows] == [fresh.id]


@pytest.mark.asyncio
async def test_a_bulk_run_skips_an_endpoint_the_core_already_routes_away(db, monkeypatch):
    core = await _core(db, "main", ["spof 401", "india"], routing_rules=[_pass_through("india")])
    await _node(db, "first", core, 62050)
    profile = await _profile(db)
    await db.commit()

    applied: list[int] = []
    _stub_apply(monkeypatch, applied)

    result = await router.create_assignments_bulk(
        AssignmentBulkPayload(profile_id=profile.id, inbound_tags=["spof 401", "india"]),
        db=db,
        admin=None,
    )

    by_tag = {outcome.inbound_tag: outcome for outcome in result.outcomes}
    assert by_tag["india"].status == "skipped"
    assert by_tag["india"].reason == capability.PRE_ROUTED
    assert by_tag["spof 401"].status == "applied"
    assert result.skipped == 1


@pytest.mark.asyncio
async def test_a_bulk_run_skips_an_endpoint_no_node_serves(db, monkeypatch):
    core = await _core(db, "main", ["in"])
    await _core(db, "spare", ["orphan-in"])
    await _node(db, "first", core, 62050)
    profile = await _profile(db)
    await db.commit()

    applied: list[int] = []
    _stub_apply(monkeypatch, applied)

    result = await router.create_assignments_bulk(
        AssignmentBulkPayload(profile_id=profile.id, inbound_tags=["orphan-in"]),
        db=db,
        admin=None,
    )

    assert [outcome.reason for outcome in result.outcomes] == [capability.NO_NODES]
    assert result.skipped == 1
    assert applied == []


@pytest.mark.asyncio
async def test_a_bulk_run_skips_a_node_whose_core_is_not_xray(db, monkeypatch):
    singbox = await _core(db, "singbox", ["sb-in"], core_type=CoreType.singbox)
    sing = await _node(db, "sing", singbox, 62050)
    profile = await _profile(db)
    await db.commit()

    applied: list[int] = []
    _stub_apply(monkeypatch, applied)

    result = await router.create_assignments_bulk(
        AssignmentBulkPayload(profile_id=profile.id, node_ids=[sing.id]),
        db=db,
        admin=None,
    )

    assert [outcome.reason for outcome in result.outcomes] == [capability.CORE_NOT_XRAY]
    assert result.skipped == 1
    assert applied == []


@pytest.mark.asyncio
async def test_a_bulk_apply_refused_by_a_shared_core_is_reported_as_skipped(db, monkeypatch):
    core = await _core(db, "shared", ["in"])
    fresh = await _node(db, "fresh", core, 62050)
    profile = await _profile(db)
    await db.commit()

    blocker = NodeCapability(
        id=63,
        name="stale",
        core_config_id=core.id,
        core_type="xray",
        node_version=OUTDATED,
        status="connected",
        supported=False,
        reason=capability.NODE_OUTDATED,
    )

    async def refuse(session, assignment, admin, allow_restart=False):
        raise service.UnsupportedNodes("core is shared with a node that cannot resolve this filter", core.id, [blocker])

    monkeypatch.setattr(service, "apply_assignment", refuse)

    result = await router.create_assignments_bulk(
        AssignmentBulkPayload(profile_id=profile.id, node_ids=[fresh.id]),
        db=db,
        admin=None,
    )

    assert [outcome.status for outcome in result.outcomes] == ["skipped"]
    assert [outcome.reason for outcome in result.outcomes] == [capability.NODE_OUTDATED]
    assert result.applied == 0


@pytest.mark.asyncio
async def test_a_version_that_cannot_be_read_is_treated_as_too_old():
    assert capability.carries_filter_assets(CURRENT) is True
    assert capability.carries_filter_assets("0.7.0") is True
    assert capability.carries_filter_assets(OUTDATED) is False
    assert capability.carries_filter_assets("") is False
    assert capability.carries_filter_assets(None) is False
    assert capability.carries_filter_assets("not-a-version") is False


@pytest.mark.asyncio
async def test_the_reason_reported_for_a_node_follows_one_order():
    assert capability.node_reason("connecting", "xray", CURRENT) == capability.NODE_DISCONNECTED
    assert capability.node_reason("connected", "singbox", OUTDATED) == capability.CORE_NOT_XRAY
    assert capability.node_reason("connected", "xray", OUTDATED) == capability.NODE_OUTDATED
    assert capability.node_reason("connected", "xray", CURRENT) is None
