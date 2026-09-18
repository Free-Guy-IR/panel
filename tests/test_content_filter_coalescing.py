import json
import re

import pytest

from app.db.models import CoreType, Node
from app.fork.content_filter import capability, service
from app.fork.content_filter.rules import (
    CATEGORY_RULE_LIMIT,
    DOMAIN_MATCHER_LIMIT,
    build_rules,
    names_a_category,
    owns_tag,
    tag_assignment_id,
)
from app.fork.models.content_filter import ContentFilterSniffingOverride

FILTER_OUTBOUNDS = [{"tag": "BLOCK", "protocol": "blackhole"}, {"tag": "DIRECT", "protocol": "freedom"}]
ADS_MATCHER = "geosite:category-ads-all"
TAG_SHAPE = re.compile(r"^pgcf-\d+-(allow|block|cat|strict)-[0-9a-f]{10}-[0-9a-f]{8}$")


class _Profile:
    def __init__(self, profile_id: int, name: str, categories: list[str] | None = None, blocked: list | None = None):
        self.id = profile_id
        self.name = name
        self.categories = list(categories or [])
        self.allow_list = []
        self.block_list = list(blocked or [])
        self.strict_mode = False


class _Assignment:
    def __init__(self, assignment_id: int, profile: _Profile, inbound_tag: str, node_id: int | None = None):
        self.id = assignment_id
        self.profile = profile
        self.profile_id = profile.id
        self.inbound_tag = inbound_tag
        self.node_id = node_id
        self.is_enabled = True
        self.enforced = True
        self.last_error = None
        self.last_checked_at = None
        self.applied_digest = None


class _Node:
    def __init__(self, node_id: int, core_id: int, status: str = "connected"):
        self.id = node_id
        self.name = f"node-{node_id}"
        self.core_config_id = core_id
        self.extra_cores: list[int] = []
        self.status = status
        self.node_version = capability.MIN_NODE_VERSION


class _Core:
    def __init__(self, core_id: int, tags: list[str], routing: list | None = None):
        self.id = core_id
        self.name = f"core-{core_id}"
        self.type = CoreType.xray
        self.config = {
            "inbounds": [
                {"tag": tag, "protocol": protocol}
                for tag, protocol in ((t if isinstance(t, tuple) else (t, "vless")) for t in tags)
            ],
            "outbounds": list(FILTER_OUTBOUNDS),
        }
        if routing is not None:
            self.config["routing"] = {"rules": list(routing)}
        self.exclude_inbound_tags = []
        self.fallbacks_inbound_tags = []


class _LiveNode:
    def __init__(self):
        self.rules: list[dict] = []
        self.added: list[str] = []
        self.removed: list[str] = []

    async def add_routing_rule(self, rule, should_reset):
        payload = json.loads(rule)
        self.rules.append(payload)
        self.added.append(str(payload.get("ruleTag") or ""))

    async def remove_routing_rule(self, rule_tag):
        self.rules = [row for row in self.rules if row.get("ruleTag") != rule_tag]
        self.removed.append(rule_tag)

    async def test_route(self, inbound_tag, network, target_domain, target_port):
        return _Verdict()

    def tags(self) -> list[str]:
        return [str(row.get("ruleTag") or "") for row in self.rules]


class _Verdict:
    outbound_tag = "BLOCK"


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _DB:
    def __init__(self, nodes, assignments):
        self.nodes = nodes
        self.assignments = assignments
        self.live: dict[int, _LiveNode] = {}
        self.overrides: list = []
        self.commits = 0

    async def execute(self, statement):
        entity = statement.column_descriptions[0].get("entity")
        params = statement.compile().params
        if entity is Node:
            core_id = params.get("core_config_id_1")
            rendered = str(statement)
            nulls = "core_config_id IS NULL" in rendered
            rows = [
                node
                for node in self.nodes
                if core_id is None or node.core_config_id == core_id or (nulls and node.core_config_id is None)
            ]
            return _Result(sorted(rows, key=lambda node: node.id))
        if entity is ContentFilterSniffingOverride:
            core_id = params.get("core_id_1")
            return _Result([row for row in self.overrides if row.core_id == core_id])
        if entity is None:
            return _Result([])
        node_id = params.get("node_id_1")
        return _Result([row for row in self.assignments if row.is_enabled and row.node_id in (None, node_id)])

    async def get(self, model, pk):
        return next((node for node in self.nodes if node.id == pk), None)

    def add(self, row):
        self.overrides.append(row)

    async def delete(self, row):
        self.overrides = [kept for kept in self.overrides if kept is not row]
        self.assignments = [kept for kept in self.assignments if kept is not row]

    async def flush(self):
        return None

    async def commit(self):
        self.commits += 1


class _Manager:
    def __init__(self, db: _DB):
        self.db = db

    async def get_node(self, node_id):
        return self.db.live.get(node_id)


def _fleet(monkeypatch, cores, nodes, assignments) -> _DB:
    by_id = {core.id: core for core in cores}

    async def core_of(db, core_id):
        return by_id.get(core_id)

    db = _DB(nodes, assignments)
    for node in nodes:
        db.live[node.id] = _LiveNode()

    async def live_rules(node_id):
        node = db.live.get(node_id)
        if node is None:
            raise service.EnforcementError(f"node {node_id} is not attached to this panel", code=404)
        return [
            {"ruleTag": str(row.get("ruleTag") or ""), "outboundTag": str(row.get("outboundTag") or "")}
            for row in node.rules
        ]

    monkeypatch.setattr(service, "get_core_config_by_id", core_of)
    monkeypatch.setattr(service, "node_manager", _Manager(db))
    monkeypatch.setattr(service, "live_rules", live_rules)
    return db


def _core_writer(monkeypatch, captured: dict):
    import app.operation.core as core_operation

    class _Operation:
        def __init__(self, operator_type=None):
            self.operator_type = operator_type

        async def modify_core(self, db_arg, core_id_arg, payload, admin):
            captured["core_id"] = core_id_arg
            captured["config"] = payload.config

    async def restart(db_arg, core_id_arg, admin):
        captured.setdefault("restarted", []).append(core_id_arg)

    monkeypatch.setattr(core_operation, "CoreOperation", _Operation)
    monkeypatch.setattr(service, "_restart_core_nodes", restart)


async def _persist(monkeypatch, db, core_id) -> dict:
    captured: dict = {}
    _core_writer(monkeypatch, captured)
    captured["changed"] = await service.persist_core_rules(db, core_id, admin=object(), allow_restart=True)
    return captured


def _category_rules(rules: list[dict]) -> list[dict]:
    return [rule for rule in rules if names_a_category(rule)]


def _written(config: dict) -> list[dict]:
    return [rule for rule in config.get("routing", {}).get("rules") or [] if owns_tag(str(rule.get("ruleTag") or ""))]


def _sixteen(monkeypatch) -> tuple[_DB, _Profile, list[_Assignment]]:
    tags = [f"VLESS HTTPUPGRADE {8080 + index}" for index in range(16)]
    core = _Core(64, tags)
    profile = _Profile(1, "ads", categories=["ads"])
    assignments = [_Assignment(64 + index, profile, tag, node_id=500) for index, tag in enumerate(tags)]
    db = _fleet(monkeypatch, [core], [_Node(500, 64)], assignments)
    return db, profile, assignments


@pytest.mark.asyncio
async def test_one_profile_on_sixteen_endpoints_becomes_one_rule_not_sixteen(monkeypatch: pytest.MonkeyPatch):
    db, _, assignments = _sixteen(monkeypatch)

    rules = await service.core_persisted_rules(db, 64)

    assert len(rules) == 1
    assert len(_category_rules(rules)) == 1
    assert rules[0]["domain"] == [ADS_MATCHER]
    assert rules[0]["inboundTag"] == [assignment.inbound_tag for assignment in assignments]
    assert len(rules[0]["inboundTag"]) == 16


@pytest.mark.asyncio
async def test_the_written_core_carries_one_copy_of_the_category_list(monkeypatch: pytest.MonkeyPatch):
    db, _, _ = _sixteen(monkeypatch)

    captured = await _persist(monkeypatch, db, 64)
    written = _written(captured["config"])

    assert len(written) == 1
    assert sum(rule["domain"].count(ADS_MATCHER) for rule in written) == 1


@pytest.mark.asyncio
async def test_dropping_one_endpoint_leaves_a_single_rule_with_the_rest(monkeypatch: pytest.MonkeyPatch):
    db, _, assignments = _sixteen(monkeypatch)
    before = (await service.core_persisted_rules(db, 64))[0]["ruleTag"]

    assignments[7].is_enabled = False
    db.assignments = [row for row in db.assignments if row.is_enabled]
    rules = await service.core_persisted_rules(db, 64)

    assert len(rules) == 1
    assert len(rules[0]["inboundTag"]) == 15
    assert assignments[7].inbound_tag not in rules[0]["inboundTag"]
    assert rules[0]["ruleTag"] != before


@pytest.mark.asyncio
async def test_a_withdrawal_rewrites_the_shared_rule_instead_of_orphaning_it(monkeypatch: pytest.MonkeyPatch):
    db, _, assignments = _sixteen(monkeypatch)
    captured: dict = {}
    _core_writer(monkeypatch, captured)

    first = await service.push_live(db, 500)
    assert len(first) == 1
    stale = first[0]["ruleTag"]

    await service.withdraw_assignment(db, assignments[0], admin=object(), allow_restart=True)

    live = db.live[500].rules
    assert len(live) == 1
    assert live[0]["ruleTag"] != stale
    assert stale in db.live[500].removed
    assert len(live[0]["inboundTag"]) == 15
    assert assignments[0].inbound_tag not in live[0]["inboundTag"]
    assert len(_written(captured["config"])) == 1


@pytest.mark.asyncio
async def test_the_live_push_installs_one_rule_for_every_endpoint(monkeypatch: pytest.MonkeyPatch):
    db, _, _ = _sixteen(monkeypatch)

    pushed = await service.push_live(db, 500)

    assert len(pushed) == 1
    assert len(db.live[500].added) == 1
    assert len(pushed[0]["inboundTag"]) == 16


@pytest.mark.asyncio
async def test_the_shared_tag_is_still_claimed_and_leads_back_to_its_assignments(monkeypatch: pytest.MonkeyPatch):
    db, _, assignments = _sixteen(monkeypatch)

    plan = await service.core_rule_plan(db, 64, assignments)
    tag = plan.rules[0]["ruleTag"]

    assert owns_tag(tag) is True
    assert TAG_SHAPE.match(tag)
    assert tag_assignment_id(tag) == min(assignment.id for assignment in assignments)
    assert plan.owners[tag] == tuple(sorted(assignment.id for assignment in assignments))
    assert await service.core_persisted_owners(db, 64) == {assignment.id for assignment in assignments}


@pytest.mark.asyncio
async def test_every_member_still_reports_the_rule_it_shares(monkeypatch: pytest.MonkeyPatch):
    db, _, assignments = _sixteen(monkeypatch)

    for assignment in assignments:
        carried = await service.rules_for_node(db, assignment, 500)
        assert len(carried) == 1
        assert assignment.inbound_tag in carried[0]["inboundTag"]

    assert len({(await service.rules_for_node(db, row, 500))[0]["ruleTag"] for row in assignments}) == 1


@pytest.mark.asyncio
async def test_a_whole_node_filter_absorbs_the_endpoint_scoped_duplicate(monkeypatch: pytest.MonkeyPatch):
    core = _Core(70, ["in-a", "in-b"])
    profile = _Profile(2, "ads", categories=["ads"])
    whole = _Assignment(90, profile, "", node_id=700)
    scoped = _Assignment(91, profile, "in-a", node_id=700)
    db = _fleet(monkeypatch, [core], [_Node(700, 70)], [whole, scoped])

    plan = await service.core_rule_plan(db, 70, [whole, scoped])

    assert len(plan.rules) == 1
    assert len(_category_rules(plan.rules)) == 1
    assert plan.rules[0]["inboundTag"] == ["in-a", "in-b"]
    assert plan.owners[plan.rules[0]["ruleTag"]] == (90, 91)


@pytest.mark.asyncio
async def test_a_whole_node_filter_keeps_an_endpoint_its_own_scope_cannot_reach(monkeypatch: pytest.MonkeyPatch):
    core = _Core(77, ["in-a", ("relay-in", "dokodemo-door")])
    profile = _Profile(6, "ads", categories=["ads"])
    whole = _Assignment(94, profile, "", node_id=770)
    scoped = _Assignment(95, profile, "relay-in", node_id=770)
    db = _fleet(monkeypatch, [core], [_Node(770, 77)], [whole, scoped])

    plan = await service.core_rule_plan(db, 77, [whole, scoped])

    assert len(plan.rules) == 1
    assert plan.rules[0]["inboundTag"] == ["in-a", "relay-in"]
    assert plan.owners[plan.rules[0]["ruleTag"]] == (94, 95)


@pytest.mark.asyncio
async def test_the_size_guard_refuses_a_runaway_block_list(monkeypatch: pytest.MonkeyPatch):
    blocked = [f"blocked-{index}.example" for index in range(DOMAIN_MATCHER_LIMIT + 1)]
    core = _Core(78, ["in-a"])
    profile = _Profile(7, "huge", blocked=blocked)
    db = _fleet(monkeypatch, [core], [_Node(780, 78)], [_Assignment(96, profile, "in-a", node_id=780)])

    captured: dict = {}
    _core_writer(monkeypatch, captured)
    with pytest.raises(service.EnforcementError) as refused:
        await service.persist_core_rules(db, 78, admin=object(), allow_restart=True)

    assert refused.value.code == 409
    assert f"{DOMAIN_MATCHER_LIMIT + 1} domain matchers" in refused.value.detail
    assert '"huge"' in refused.value.detail
    assert "config" not in captured


@pytest.mark.asyncio
async def test_different_profiles_on_one_core_keep_their_own_rules(monkeypatch: pytest.MonkeyPatch):
    core = _Core(71, ["in-a", "in-b"])
    ads = _Profile(3, "ads", categories=["ads"])
    adult = _Profile(4, "adult", categories=["adult"])
    first = _Assignment(92, ads, "in-a", node_id=710)
    second = _Assignment(93, adult, "in-b", node_id=710)
    db = _fleet(monkeypatch, [core], [_Node(710, 71)], [first, second])

    rules = await service.core_persisted_rules(db, 71)

    assert len(_category_rules(rules)) == 2
    assert {rule["inboundTag"][0] for rule in rules} == {"in-a", "in-b"}
    assert len({rule["ruleTag"] for rule in rules}) == 2


@pytest.mark.asyncio
async def test_the_size_guard_refuses_a_core_that_would_carry_too_many_category_rules(
    monkeypatch: pytest.MonkeyPatch,
):
    count = CATEGORY_RULE_LIMIT + 1
    tags = [f"in-{index}" for index in range(count)]
    core = _Core(72, tags)
    assignments = [
        _Assignment(100 + index, _Profile(10 + index, f"profile-{index}", categories=["ads"]), tag, node_id=720)
        for index, tag in enumerate(tags)
    ]
    db = _fleet(monkeypatch, [core], [_Node(720, 72)], assignments)

    captured: dict = {}
    _core_writer(monkeypatch, captured)
    with pytest.raises(service.EnforcementError) as refused:
        await service.persist_core_rules(db, 72, admin=object(), allow_restart=True)

    assert refused.value.code == 409
    assert f"{count} rules" in refused.value.detail
    assert str(CATEGORY_RULE_LIMIT) in refused.value.detail
    assert '"profile-0"' in refused.value.detail
    assert "Nothing was written" in refused.value.detail
    assert "config" not in captured


@pytest.mark.asyncio
async def test_the_size_guard_stops_the_live_push_as_well(monkeypatch: pytest.MonkeyPatch):
    count = CATEGORY_RULE_LIMIT + 1
    tags = [f"in-{index}" for index in range(count)]
    core = _Core(73, tags)
    assignments = [
        _Assignment(110 + index, _Profile(20 + index, f"profile-{index}", categories=["ads"]), tag, node_id=730)
        for index, tag in enumerate(tags)
    ]
    db = _fleet(monkeypatch, [core], [_Node(730, 73)], assignments)

    with pytest.raises(service.EnforcementError) as refused:
        await service.push_live(db, 730)

    assert refused.value.code == 409
    assert db.live[730].added == []


@pytest.mark.asyncio
async def test_a_core_already_poisoned_with_one_rule_per_assignment_is_cleaned(monkeypatch: pytest.MonkeyPatch):
    tags = [f"VLESS HTTPUPGRADE {8080 + index}" for index in range(16)]
    poisoned = [build_rules(64 + index, [tag], ["ads"], [], [], False)[0] for index, tag in enumerate(tags)]
    core = _Core(74, tags, routing=poisoned)
    profile = _Profile(5, "ads", categories=["ads"])
    assignments = [_Assignment(64 + index, profile, tag, node_id=740) for index, tag in enumerate(tags)]
    db = _fleet(monkeypatch, [core], [_Node(740, 74)], assignments)

    assert len(_category_rules(poisoned)) == 16

    captured = await _persist(monkeypatch, db, 74)
    written = _written(captured["config"])

    assert len(written) == 1
    assert len(written[0]["inboundTag"]) == 16
    assert {rule["ruleTag"] for rule in poisoned}.isdisjoint({rule["ruleTag"] for rule in written})


@pytest.mark.asyncio
async def test_a_poisoned_core_can_still_be_emptied(monkeypatch: pytest.MonkeyPatch):
    tags = [f"in-{index}" for index in range(16)]
    poisoned = [build_rules(200 + index, [tag], ["ads"], [], [], False)[0] for index, tag in enumerate(tags)]
    core = _Core(75, tags, routing=poisoned)
    db = _fleet(monkeypatch, [core], [_Node(750, 75)], [])

    captured = await _persist(monkeypatch, db, 75)

    assert _written(captured["config"]) == []


@pytest.mark.asyncio
async def test_the_guard_never_blocks_a_write_that_shrinks_an_oversized_core(monkeypatch: pytest.MonkeyPatch):
    count = CATEGORY_RULE_LIMIT + 1
    tags = [f"in-{index}" for index in range(count)]
    held = [build_rules(300 + index, [tag], ["ads"], [], [], False)[0] for index, tag in enumerate([*tags, "in-extra"])]
    core = _Core(76, tags, routing=held)
    assignments = [
        _Assignment(300 + index, _Profile(30 + index, f"profile-{index}", categories=["ads"]), tag, node_id=760)
        for index, tag in enumerate(tags)
    ]
    db = _fleet(monkeypatch, [core], [_Node(760, 76)], assignments)

    captured = await _persist(monkeypatch, db, 76)

    assert len(_category_rules(_written(captured["config"]))) == count
