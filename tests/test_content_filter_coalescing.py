import json
import re

import pytest

from app.db.models import CoreType, Node
from app.fork.content_filter import capability, catalog, service
from app.fork.content_filter.rules import (
    DOMAIN_BUDGET,
    LITERAL_MATCHER_WEIGHT,
    build_rules,
    names_a_category,
    owns_tag,
    rule_coverage,
    rule_set_cost,
    tag_assignment_id,
    within_coverage,
)
from app.fork.models.content_filter import ContentFilterSniffingOverride

FILTER_OUTBOUNDS = [{"tag": "BLOCK", "protocol": "blackhole"}, {"tag": "DIRECT", "protocol": "freedom"}]
ADS_MATCHER = "geosite:category-ads-all"
TAG_SHAPE = re.compile(r"^pgcf-\d+-(allow|block|cat|strict)-[0-9a-f]{10}-[0-9a-f]{8}$")


class _Profile:
    def __init__(
        self,
        profile_id: int,
        name: str,
        categories: list[str] | None = None,
        blocked: list | None = None,
        allowed: list | None = None,
    ):
        self.id = profile_id
        self.name = name
        self.categories = list(categories or [])
        self.allow_list = list(allowed or [])
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
    def __init__(self, node_id: int, core_id: int, status: str = "connected", node_version: str | None = None):
        self.id = node_id
        self.name = f"node-{node_id}"
        self.core_config_id = core_id
        self.extra_cores: list[int] = []
        self.status = status
        self.node_version = node_version or capability.MIN_NODE_VERSION


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
    assert ADS_MATCHER in rules[0]["domain"]
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
    blocked = [f"blocked-{index}.example" for index in range(DOMAIN_BUDGET + 1)]
    core = _Core(78, ["in-a"])
    profile = _Profile(7, "huge", blocked=blocked)
    db = _fleet(monkeypatch, [core], [_Node(780, 78)], [_Assignment(96, profile, "in-a", node_id=780)])

    captured: dict = {}
    _core_writer(monkeypatch, captured)
    with pytest.raises(service.EnforcementError) as refused:
        await service.persist_core_rules(db, 78, admin=object(), allow_restart=True)

    assert refused.value.code == 409
    assert f"{DOMAIN_BUDGET + 1:,} destinations" in refused.value.detail
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
async def test_the_size_guard_refuses_a_core_that_would_load_too_many_destinations(
    monkeypatch: pytest.MonkeyPatch,
):
    count = 13
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
    assert f"{DOMAIN_BUDGET:,}" in refused.value.detail
    assert '"profile-0"' in refused.value.detail
    assert "Nothing was written" in refused.value.detail
    assert "config" not in captured


@pytest.mark.asyncio
async def test_the_size_guard_stops_the_live_push_as_well(monkeypatch: pytest.MonkeyPatch):
    count = 13
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
    count = 13
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


OUTDATED = "0.6.0"
OPERATOR_SCOPE = ["not-filtered"]
SHOP = "domain:shop.example"


def _operator_domain_category(index: int) -> dict:
    return {
        "type": "field",
        "ruleTag": f"op-domain-{index}",
        "inboundTag": list(OPERATOR_SCOPE),
        "domain": [f"ext:pgfilter.dat:pglist-{index}"],
        "outboundTag": "BLOCK",
    }


def _operator_address_category(index: int) -> dict:
    return {
        "type": "field",
        "ruleTag": f"op-address-{index}",
        "inboundTag": list(OPERATOR_SCOPE),
        "ip": [f"geoip:list-{index}"],
        "outboundTag": "BLOCK",
    }


def _operator_categories_weighing(target: int) -> list[dict]:
    from app.fork.content_filter.rules import rule_set_cost

    built: list[dict] = []
    for index in range(1, 43):
        candidate = _operator_domain_category(index)
        if rule_set_cost([*built, candidate])[0] > target:
            continue
        built.append(candidate)
    return built


def _operator_address_categories_weighing(target: int) -> list[dict]:

    built = _operator_categories_weighing(target)
    return [*built, *(_operator_address_category(index) for index in range(len(built)))]


def _operator_bulk_to(target: int, held: list[dict]) -> list[dict]:
    from app.fork.content_filter.rules import rule_set_cost

    short = target - rule_set_cost(held)[0]
    if short <= 0:
        return held
    return [
        *held,
        {
            "type": "field",
            "ruleTag": "op-bulk",
            "inboundTag": list(OPERATOR_SCOPE),
            "domain": [f"top-up-{index}.example" for index in range(short)],
            "outboundTag": "BLOCK",
        },
    ]


async def _persist_dropping(monkeypatch, db, core_id) -> dict:
    captured: dict = {}
    _core_writer(monkeypatch, captured)
    captured["changed"] = await service.persist_core_rules(
        db, core_id, admin=object(), allow_restart=True, drop_unsupported=True
    )
    return captured


def _hold(core: _Core, rules: list[dict]) -> None:
    routing = core.config.setdefault("routing", {})
    routing["rules"] = [*(routing.get("rules") or []), *rules]


def _first_match(rules: list[dict], inbound_tag: str, matcher: str) -> str:
    for rule in rules:
        if inbound_tag not in (rule.get("inboundTag") or []):
            continue
        if matcher in (rule.get("domain") or []):
            return str(rule.get("outboundTag") or "")
    return "unmatched"


@pytest.mark.asyncio
async def test_a_withdrawal_keeps_the_other_endpoints_filtered_on_a_core_with_a_stale_node(
    monkeypatch: pytest.MonkeyPatch,
):
    tags = [f"VLESS HTTPUPGRADE {8080 + index}" for index in range(16)]
    core = _Core(80, tags)
    profile = _Profile(40, "ads", categories=["ads"])
    assignments = [_Assignment(400 + index, profile, tag) for index, tag in enumerate(tags)]
    db = _fleet(monkeypatch, [core], [_Node(800, 80), _Node(801, 80, node_version=OUTDATED)], assignments)
    stored = await service.core_persisted_rules(db, 80)
    _hold(core, stored)

    captured: dict = {}
    _core_writer(monkeypatch, captured)
    await service.withdraw_assignment(db, assignments[7], admin=object(), allow_restart=True)
    written = _written(captured["config"])

    assert len(written) == 1
    assert len(written[0]["inboundTag"]) == 15
    assert assignments[7].inbound_tag not in written[0]["inboundTag"]
    assert written[0]["ruleTag"] != stored[0]["ruleTag"]
    assert ADS_MATCHER in written[0]["domain"]


@pytest.mark.asyncio
async def test_a_disabled_member_does_not_un_filter_the_rest_on_a_core_with_a_stale_node(
    monkeypatch: pytest.MonkeyPatch,
):
    tags = ["in-a", "in-b", "in-c"]
    core = _Core(84, tags)
    profile = _Profile(45, "ads", categories=["ads"])
    assignments = [_Assignment(440 + index, profile, tag) for index, tag in enumerate(tags)]
    db = _fleet(monkeypatch, [core], [_Node(840, 84), _Node(841, 84, status="connecting")], assignments)
    _hold(core, await service.core_persisted_rules(db, 84))

    assignments[0].is_enabled = False
    written = _written((await _persist_dropping(monkeypatch, db, 84))["config"])

    assert len(written) == 1
    assert written[0]["inboundTag"] == ["in-b", "in-c"]


@pytest.mark.asyncio
async def test_a_narrowed_rewrite_never_reaches_the_unsupported_node_screen(monkeypatch: pytest.MonkeyPatch):
    tags = ["in-a", "in-b"]
    core = _Core(85, tags)
    profile = _Profile(46, "ads", categories=["ads"])
    assignments = [_Assignment(450 + index, profile, tag) for index, tag in enumerate(tags)]
    db = _fleet(monkeypatch, [core], [_Node(850, 85), _Node(851, 85, node_version=OUTDATED)], assignments)
    _hold(core, await service.core_persisted_rules(db, 85))

    assignments[1].is_enabled = False
    advisories: list[str] = []
    captured: dict = {}
    _core_writer(monkeypatch, captured)
    await service.persist_core_rules(db, 85, admin=object(), allow_restart=True, advisories=advisories)

    assert _written(captured["config"])[0]["inboundTag"] == ["in-a"]
    assert advisories == []


@pytest.mark.asyncio
async def test_a_brand_new_profile_is_still_kept_out_of_a_core_with_a_stale_node(monkeypatch: pytest.MonkeyPatch):
    core = _Core(86, ["in-a", "in-b"])
    ads = _Profile(47, "ads", categories=["ads"])
    adult = _Profile(48, "adult", categories=["adult"])
    held = _Assignment(460, ads, "in-a")
    db = _fleet(monkeypatch, [core], [_Node(860, 86), _Node(861, 86, node_version=OUTDATED)], [held])
    _hold(core, await service.core_persisted_rules(db, 86))
    db.assignments = [held, _Assignment(461, adult, "in-b")]

    with pytest.raises(service.UnsupportedNodes) as refused:
        await service.persist_core_rules(db, 86, admin=object(), allow_restart=True)

    assert refused.value.code == 409

    captured = await _persist_dropping(monkeypatch, db, 86)
    written = _written(captured.get("config") or core.config)

    assert [rule["inboundTag"] for rule in written] == [["in-a"]]


@pytest.mark.asyncio
async def test_one_rule_that_reaches_a_new_endpoint_still_counts_as_an_addition(monkeypatch: pytest.MonkeyPatch):
    core = _Core(87, ["in-a", "in-b"])
    profile = _Profile(49, "ads", categories=["ads"])
    held = _Assignment(470, profile, "in-a")
    db = _fleet(monkeypatch, [core], [_Node(870, 87), _Node(871, 87, node_version=OUTDATED)], [held])
    _hold(core, await service.core_persisted_rules(db, 87))
    db.assignments = [held, _Assignment(471, profile, "in-b")]

    assert len(await service.core_persisted_rules(db, 87)) == 1

    with pytest.raises(service.UnsupportedNodes):
        await service.persist_core_rules(db, 87, admin=object(), allow_restart=True)


@pytest.mark.asyncio
async def test_a_wider_matcher_on_the_same_endpoint_still_counts_as_an_addition(monkeypatch: pytest.MonkeyPatch):
    core = _Core(88, ["in-a"])
    narrow = _Profile(50, "narrow", blocked=["ads.example"])
    wide = _Profile(51, "wide", categories=["ads"])
    db = _fleet(monkeypatch, [core], [_Node(880, 88), _Node(881, 88, node_version=OUTDATED)], [])
    db.assignments = [_Assignment(480, narrow, "in-a")]
    _hold(core, await service.core_persisted_rules(db, 88))
    db.assignments = [_Assignment(481, wide, "in-a")]

    with pytest.raises(service.UnsupportedNodes):
        await service.persist_core_rules(db, 88, admin=object(), allow_restart=True)


@pytest.mark.asyncio
async def test_the_size_guard_counts_the_category_rules_the_operator_wrote(monkeypatch: pytest.MonkeyPatch):
    held = _operator_bulk_to(DOMAIN_BUDGET - 1, _operator_categories_weighing(DOMAIN_BUDGET - 1))
    core = _Core(89, ["in-a"], routing=held)
    profile = _Profile(52, "ads", categories=["ads"])
    db = _fleet(monkeypatch, [core], [_Node(890, 89)], [_Assignment(490, profile, "in-a", node_id=890)])

    captured: dict = {}
    _core_writer(monkeypatch, captured)
    with pytest.raises(service.EnforcementError) as refused:
        await service.persist_core_rules(db, 89, admin=object(), allow_restart=True)

    assert refused.value.code == 409
    assert "destinations" in refused.value.detail
    assert "the rules the core config already carries" in refused.value.detail
    assert '"ads"' in refused.value.detail
    assert "Nothing was written" in refused.value.detail
    assert "config" not in captured


@pytest.mark.asyncio
async def test_the_size_guard_counts_a_category_the_operator_named_by_address(monkeypatch: pytest.MonkeyPatch):
    held = _operator_bulk_to(DOMAIN_BUDGET - 1, _operator_address_categories_weighing(DOMAIN_BUDGET - 1))
    core = _Core(90, ["in-a"], routing=held)
    profile = _Profile(53, "ads", categories=["ads"])
    db = _fleet(monkeypatch, [core], [_Node(900, 90)], [_Assignment(500, profile, "in-a", node_id=900)])

    captured: dict = {}
    _core_writer(monkeypatch, captured)
    with pytest.raises(service.EnforcementError) as refused:
        await service.persist_core_rules(db, 90, admin=object(), allow_restart=True)

    assert refused.value.code == 409
    assert "destinations" in refused.value.detail
    assert "the rules the core config already carries" in refused.value.detail
    assert "config" not in captured


@pytest.mark.asyncio
async def test_the_size_guard_counts_the_operator_rules_on_the_live_push(monkeypatch: pytest.MonkeyPatch):
    held = _operator_bulk_to(DOMAIN_BUDGET - 1, _operator_categories_weighing(DOMAIN_BUDGET - 1))
    core = _Core(91, ["in-a"], routing=held)
    profile = _Profile(54, "ads", categories=["ads"])
    db = _fleet(monkeypatch, [core], [_Node(910, 91)], [_Assignment(510, profile, "in-a", node_id=910)])

    with pytest.raises(service.EnforcementError) as refused:
        await service.push_live(db, 910)

    assert refused.value.code == 409
    assert db.live[910].added == []


@pytest.mark.asyncio
async def test_the_operator_matcher_count_is_carried_into_the_domain_limit(monkeypatch: pytest.MonkeyPatch):
    held = _operator_bulk_to(DOMAIN_BUDGET, _operator_categories_weighing(DOMAIN_BUDGET))
    core = _Core(92, ["in-a"], routing=held)
    profile = _Profile(55, "small", blocked=["one.example"])
    db = _fleet(monkeypatch, [core], [_Node(920, 92)], [_Assignment(520, profile, "in-a", node_id=920)])

    captured: dict = {}
    _core_writer(monkeypatch, captured)
    with pytest.raises(service.EnforcementError) as refused:
        await service.persist_core_rules(db, 92, admin=object(), allow_restart=True)

    assert refused.value.code == 409
    assert "destinations" in refused.value.detail
    assert "the rules the core config already carries" in refused.value.detail
    assert '1 of those come from "small"' in refused.value.detail
    assert "config" not in captured


@pytest.mark.asyncio
async def test_a_withdrawal_still_works_on_a_core_the_operator_pushed_over_the_limit(
    monkeypatch: pytest.MonkeyPatch,
):
    held = _operator_categories_weighing(DOMAIN_BUDGET)
    core = _Core(93, ["in-a", "in-b"], routing=held)
    profile = _Profile(56, "ads", categories=["ads"])
    assignments = [_Assignment(530, profile, "in-a", node_id=930), _Assignment(531, profile, "in-b", node_id=930)]
    db = _fleet(monkeypatch, [core], [_Node(930, 93)], assignments)
    _hold(core, await service.core_persisted_rules(db, 93))

    captured: dict = {}
    _core_writer(monkeypatch, captured)
    await service.withdraw_assignment(db, assignments[0], admin=object(), allow_restart=True)
    written = _written(captured["config"])

    assert len(written) == 1
    assert written[0]["inboundTag"] == ["in-b"]


@pytest.mark.asyncio
async def test_disabling_a_filter_still_works_on_a_core_the_operator_pushed_over_the_limit(
    monkeypatch: pytest.MonkeyPatch,
):
    held = _operator_categories_weighing(DOMAIN_BUDGET)
    core = _Core(94, ["in-a"], routing=held)
    profile = _Profile(57, "ads", categories=["ads"])
    assignment = _Assignment(540, profile, "in-a", node_id=940)
    db = _fleet(monkeypatch, [core], [_Node(940, 94)], [assignment])
    _hold(core, await service.core_persisted_rules(db, 94))

    assignment.is_enabled = False
    captured = await _persist_dropping(monkeypatch, db, 94)

    assert _written(captured["config"]) == []
    assert len(captured["config"]["routing"]["rules"]) == len(held)


@pytest.mark.asyncio
async def test_deleting_the_profile_still_works_on_a_core_the_operator_pushed_over_the_limit(
    monkeypatch: pytest.MonkeyPatch,
):
    held = _operator_categories_weighing(DOMAIN_BUDGET)
    core = _Core(95, ["in-a"], routing=held)
    profile = _Profile(58, "ads", categories=["ads"])
    db = _fleet(monkeypatch, [core], [_Node(950, 95)], [_Assignment(550, profile, "in-a", node_id=950)])
    _hold(core, await service.core_persisted_rules(db, 95))
    db.assignments = []

    captured = await _persist_dropping(monkeypatch, db, 95)

    assert _written(captured["config"]) == []
    assert len(captured["config"]["routing"]["rules"]) == len(held)


@pytest.mark.asyncio
async def test_a_poisoned_core_the_operator_also_loaded_can_still_be_cleaned(monkeypatch: pytest.MonkeyPatch):
    tags = [f"in-{index}" for index in range(16)]
    poisoned = [build_rules(600 + index, [tag], ["ads"], [], [], False)[0] for index, tag in enumerate(tags)]
    held = _operator_categories_weighing(DOMAIN_BUDGET)
    core = _Core(96, tags, routing=[*held, *poisoned])
    profile = _Profile(59, "ads", categories=["ads"])
    assignments = [_Assignment(600 + index, profile, tag, node_id=960) for index, tag in enumerate(tags)]
    db = _fleet(monkeypatch, [core], [_Node(960, 96)], assignments)

    written = _written((await _persist_dropping(monkeypatch, db, 96))["config"])

    assert len(written) == 1
    assert len(written[0]["inboundTag"]) == 16


@pytest.mark.asyncio
async def test_a_profile_split_by_another_profile_keeps_the_verdict_it_had(monkeypatch: pytest.MonkeyPatch):
    core = _Core(97, ["in-a", "in-b"])
    allowing = _Profile(60, "shop-allowed", allowed=["shop.example"])
    blocking = _Profile(61, "shop-blocked", blocked=["shop.example"])
    assignments = [
        _Assignment(700, allowing, "in-a", node_id=970),
        _Assignment(701, blocking, "", node_id=970),
        _Assignment(702, allowing, "in-b", node_id=970),
    ]
    db = _fleet(monkeypatch, [core], [_Node(970, 97)], assignments)

    rules = await service.core_persisted_rules(db, 97)

    assert _first_match(rules, "in-b", SHOP) == "BLOCK"
    assert _first_match(rules, "in-a", SHOP) == "DIRECT"
    assert [rule["inboundTag"] for rule in rules] == [["in-a"], ["in-a", "in-b"], ["in-b"]]


@pytest.mark.asyncio
async def test_the_split_profile_still_owns_both_of_its_rules(monkeypatch: pytest.MonkeyPatch):
    core = _Core(98, ["in-a", "in-b"])
    allowing = _Profile(62, "shop-allowed", allowed=["shop.example"])
    blocking = _Profile(63, "shop-blocked", blocked=["shop.example"])
    assignments = [
        _Assignment(710, allowing, "in-a", node_id=980),
        _Assignment(711, blocking, "", node_id=980),
        _Assignment(712, allowing, "in-b", node_id=980),
    ]
    db = _fleet(monkeypatch, [core], [_Node(980, 98)], assignments)

    plan = await service.core_rule_plan(db, 98, assignments)
    mine = [rule for rule in plan.rules if 712 in plan.owners[rule["ruleTag"]]]

    assert len(mine) == 1
    assert mine[0]["inboundTag"] == ["in-b"]
    assert await service.core_persisted_owners(db, 98) == {710, 711, 712}
    assert {plan.labels[rule["ruleTag"]] for rule in plan.rules} == {"shop-allowed", "shop-blocked"}


@pytest.mark.asyncio
async def test_a_profile_no_other_rule_interleaves_still_collapses_to_one_rule(monkeypatch: pytest.MonkeyPatch):
    tags = [f"in-{index}" for index in range(16)]
    core = _Core(99, [*tags, "in-other"])
    ads = _Profile(64, "ads", categories=["ads"])
    adult = _Profile(65, "adult", categories=["adult"])
    assignments = [_Assignment(720 + index, ads, tag, node_id=990) for index, tag in enumerate(tags)]
    assignments.append(_Assignment(760, adult, "in-other", node_id=990))
    db = _fleet(monkeypatch, [core], [_Node(990, 99)], assignments)

    rules = await service.core_persisted_rules(db, 99)

    assert len(rules) == 2
    assert len(rules[0]["inboundTag"]) == 16
    assert rules[1]["inboundTag"] == ["in-other"]


@pytest.mark.asyncio
async def test_a_profile_behind_another_one_still_collapses_to_one_rule(monkeypatch: pytest.MonkeyPatch):
    tags = [f"in-{index}" for index in range(16)]
    core = _Core(101, tags)
    adult = _Profile(66, "adult", categories=["adult"])
    ads = _Profile(67, "ads", categories=["ads"])
    assignments = [_Assignment(770, adult, "", node_id=1010)]
    assignments += [_Assignment(780 + index, ads, tag, node_id=1010) for index, tag in enumerate(tags)]
    db = _fleet(monkeypatch, [core], [_Node(1010, 101)], assignments)

    rules = await service.core_persisted_rules(db, 101)

    assert len(rules) == 2
    assert len(rules[0]["inboundTag"]) == 16
    assert len(rules[1]["inboundTag"]) == 16
    assert ADS_MATCHER in rules[1]["domain"]


def test_the_cost_of_a_rule_set_sees_a_category_named_by_address():
    heavy = catalog.size_of("pglist-1")

    assert heavy > LITERAL_MATCHER_WEIGHT
    assert rule_set_cost([{"ip": ["geoip:cn"]}]) == (1, 1)
    assert rule_set_cost([{"ip": ["ext:pgfilter.dat:pglist-1"]}]) == (heavy, 1)
    assert rule_set_cost([{"ip": ["ext-ip:pgfilter.dat:pglist-1"]}]) == (heavy, 1)
    assert rule_set_cost([{"source": ["geoip:ir"]}]) == (1, 1)
    assert rule_set_cost([{"ip": ["0.0.0.0/0", "::/0"]}]) == (2, 2)
    assert names_a_category({"ip": ["geoip:private"]}) is True


def test_the_cost_of_a_rule_set_sees_the_other_domain_list_spellings():
    heavy = catalog.size_of("pglist-1")
    ads = next(group.size for group in catalog.groups() if group.geosite == "category-ads-all")

    assert rule_set_cost([{"domain": ["ext-domain:pgfilter.dat:pglist-1"]}]) == (heavy, 1)
    assert rule_set_cost([{"domains": ["geosite:category-ads-all"]}]) == (ads, 1)
    assert rule_set_cost([{"domain": ["GEOSITE:category-ads-all"]}]) == (ads, 1)
    assert rule_set_cost([{"domain": ["regexp:^ads\\."]}]) == (1, 1)
    assert rule_set_cost([{"domain": ["ads.example"]}]) == (1, 1)


def test_coverage_says_a_shrunken_rewrite_adds_nothing():
    before = build_rules(1, ["in-a", "in-b", "in-c"], ["ads"], [], [], False)
    after = build_rules(2, ["in-a", "in-c"], ["ads"], [], [], False)

    assert before[0]["ruleTag"] != after[0]["ruleTag"]
    assert within_coverage(rule_coverage(before), after) is True
    assert within_coverage(rule_coverage(after), before) is False


def test_coverage_says_a_swap_for_a_broader_rule_is_an_addition():
    before = build_rules(1, ["in-a"], [], [], ["ads.example"], False)
    after = build_rules(1, ["in-a"], ["ads"], [], [], False)

    assert len(before) == len(after) == 1
    assert within_coverage(rule_coverage(before), after) is False


def test_coverage_treats_an_unscoped_rule_as_covering_every_endpoint():
    everywhere = build_rules(1, [], ["ads"], [], [], False)
    scoped = build_rules(1, ["in-a"], ["ads"], [], [], False)

    assert "inboundTag" not in everywhere[0]
    assert within_coverage(rule_coverage(everywhere), scoped) is True
    assert within_coverage(rule_coverage(scoped), everywhere) is False


def test_a_dropped_constraint_broadens_and_an_added_one_narrows():
    restricted = {
        "type": "field",
        "ruleTag": "pgcf-1-block-0123456789-abcdef01",
        "domain": ["domain:ads.example"],
        "source": ["10.0.0.0/8"],
        "outboundTag": "BLOCK",
    }
    unrestricted = {
        "type": "field",
        "ruleTag": "pgcf-1-block-9876543210-10fedcba",
        "domain": ["domain:ads.example"],
        "outboundTag": "BLOCK",
    }

    assert within_coverage(rule_coverage([restricted]), [unrestricted]) is False
    assert within_coverage(rule_coverage([unrestricted]), [restricted]) is True


def test_coverage_reads_a_numeric_port_without_crashing():
    numeric = {"type": "field", "port": 443, "network": "tcp", "outboundTag": "BLOCK"}
    spelled = {"type": "field", "port": "443", "network": "tcp", "outboundTag": "BLOCK"}

    assert rule_coverage([numeric]) == rule_coverage([spelled])


def test_coverage_never_reads_a_dropped_destination_field_as_a_narrowing():
    both = {"type": "field", "domain": ["ads.example"], "ip": ["1.2.3.0/24"], "outboundTag": "BLOCK"}
    domain_only = {"type": "field", "domain": ["ads.example"], "outboundTag": "BLOCK"}
    address_only = {"type": "field", "ip": ["1.2.3.0/24"], "outboundTag": "BLOCK"}

    assert within_coverage(rule_coverage([both]), [domain_only]) is False
    assert within_coverage(rule_coverage([both]), [address_only]) is False


def test_an_endpoint_literally_named_star_covers_only_itself():
    starred = {"type": "field", "domain": ["a.example"], "inboundTag": ["*"], "outboundTag": "BLOCK"}
    elsewhere = {"type": "field", "domain": ["a.example"], "inboundTag": ["real-tag"], "outboundTag": "BLOCK"}
    unscoped = {"type": "field", "domain": ["a.example"], "outboundTag": "BLOCK"}

    assert within_coverage(rule_coverage([starred]), [elsewhere]) is False
    assert within_coverage(rule_coverage([unscoped]), [elsewhere]) is True


def test_two_stored_rules_do_not_jointly_cover_a_third_combination():
    stored_a = {"type": "field", "domain": ["a.example"], "ip": ["1.1.1.0/24"], "outboundTag": "BLOCK"}
    stored_b = {"type": "field", "domain": ["b.example"], "ip": ["2.2.2.0/24"], "outboundTag": "BLOCK"}
    crossed = {"type": "field", "domain": ["a.example"], "ip": ["2.2.2.0/24"], "outboundTag": "BLOCK"}

    assert within_coverage(rule_coverage([stored_a, stored_b]), [crossed]) is False
    assert within_coverage(rule_coverage([stored_a, stored_b]), [stored_a]) is True


def test_coverage_reads_a_wider_matcher_list_as_an_addition():
    narrow = {"type": "field", "domain": ["a.example"], "inboundTag": ["x"], "outboundTag": "BLOCK"}
    wider_domains = {"type": "field", "domain": ["a.example", "b.example"], "inboundTag": ["x"], "outboundTag": "BLOCK"}
    wider_scope = {"type": "field", "domain": ["a.example"], "inboundTag": ["x", "y"], "outboundTag": "BLOCK"}

    assert within_coverage(rule_coverage([narrow]), [wider_domains]) is False
    assert within_coverage(rule_coverage([narrow]), [wider_scope]) is False


def test_merging_two_already_protected_endpoints_is_not_an_addition():
    one = {"type": "field", "domain": ["ads.example"], "inboundTag": ["in-a"], "outboundTag": "BLOCK"}
    other = {"type": "field", "domain": ["ads.example"], "inboundTag": ["in-b"], "outboundTag": "BLOCK"}
    merged = {"type": "field", "domain": ["ads.example"], "inboundTag": ["in-a", "in-b"], "outboundTag": "BLOCK"}
    reaching_further = {
        "type": "field",
        "domain": ["ads.example"],
        "inboundTag": ["in-a", "in-c"],
        "outboundTag": "BLOCK",
    }

    assert within_coverage(rule_coverage([one, other]), [merged]) is True
    assert within_coverage(rule_coverage([one, other]), [reaching_further]) is False


def test_any_dropped_restriction_counts_even_one_this_code_has_never_heard_of():
    for field, value in (
        ("localPort", ["443"]),
        ("port", [443]),
        ("network", ["tcp"]),
        ("aFieldFromAFutureXray", ["x"]),
    ):
        held = {"type": "field", "domain": ["ads.example"], field: value, "outboundTag": "BLOCK"}
        without = {"type": "field", "domain": ["ads.example"], "outboundTag": "BLOCK"}
        assert within_coverage(rule_coverage([held]), [without]) is False


def test_coverage_keeps_the_case_of_values_where_case_decides_the_match():
    upper = {"type": "field", "domain": [r"regexp:^\D+\.example$"], "outboundTag": "BLOCK"}
    lower = {"type": "field", "domain": [r"regexp:^\d+\.example$"], "outboundTag": "BLOCK"}
    listed = {"type": "field", "domain": ["ext:pgfilter.dat:Ads"], "outboundTag": "BLOCK"}
    listed_lower = {"type": "field", "domain": ["ext:pgfilter.dat:ads"], "outboundTag": "BLOCK"}

    assert within_coverage(rule_coverage([upper]), [lower]) is False
    assert within_coverage(rule_coverage([upper]), [upper]) is True
    assert within_coverage(rule_coverage([listed]), [listed_lower]) is False
    assert within_coverage(rule_coverage([listed]), [listed]) is True


def test_coverage_still_ignores_case_where_the_core_does():
    mixed = {"type": "field", "domain": ["Ads.Example", "geosite:Category-Ads-All"], "outboundTag": "BLOCK"}
    plain = {"type": "field", "domain": ["ads.example", "geosite:category-ads-all"], "outboundTag": "BLOCK"}

    assert within_coverage(rule_coverage([mixed]), [plain]) is True
    assert within_coverage(rule_coverage([plain]), [mixed]) is True


def test_a_rules_cost_is_the_destinations_it_makes_a_node_load():
    from app.fork.content_filter.rules import matcher_weight

    assert matcher_weight("geosite:category-ads-all") == 170624
    assert matcher_weight("geosite:category-porn") == 6635
    assert matcher_weight("ext:pgfilter.dat:pglist-1") == 178876
    assert matcher_weight("domain:example.com") == 1
    assert matcher_weight("geoip:private") == 1


def test_two_real_profiles_fit_where_sixteen_copies_of_one_do_not():
    from app.fork.content_filter import service
    from app.fork.content_filter.rules import build_rules

    operator = [{"type": "field", "ip": ["geoip:private"], "outboundTag": "BLOCK"}]
    ads = build_rules(6, ["spof 401"], ["ads"], [], [], False)
    kids = build_rules(8, ["spof 402"], ["ads", "adult"], [], [], False)
    sixteen = [
        rule for index in range(16) for rule in build_rules(100 + index, [f"tag{index}"], ["ads"], [], [], False)
    ]

    service.guard_rule_size("node 10", [*ads, *kids], [*operator, *ads], {})

    with pytest.raises(service.EnforcementError) as refused:
        service.guard_rule_size("node 10", sixteen, operator, {})
    assert refused.value.code == 409


def test_an_oversized_core_can_always_be_shrunk():
    from app.fork.content_filter import service
    from app.fork.content_filter.rules import build_rules

    operator = [{"type": "field", "ip": ["geoip:private"], "outboundTag": "BLOCK"}]
    sixteen = [
        rule for index in range(16) for rule in build_rules(100 + index, [f"tag{index}"], ["ads"], [], [], False)
    ]
    poisoned = [*operator, *sixteen]

    service.guard_rule_size("node 10", [], poisoned, {})
    service.guard_rule_size("node 10", build_rules(6, ["spof 401"], ["ads"], [], [], False), poisoned, {})

    with pytest.raises(service.EnforcementError):
        service.guard_rule_size("node 10", [*sixteen, *build_rules(6, ["x"], ["ads"], [], [], False)], poisoned, {})
