import json

import pytest
from PasarGuardNodeBridge import NodeAPIError

from app.db.models import CoreType, Node
from app.fork.content_filter import capability, service
from app.fork.content_filter.rules import owns_tag
from app.fork.models.content_filter import ContentFilterSniffingOverride

FILTER_OUTBOUNDS = [{"tag": "BLOCK", "protocol": "blackhole"}, {"tag": "DIRECT", "protocol": "freedom"}]
DUPLICATE_RULE_TAG = "app/router: duplicate ruleTag "
MISSING_RULE_TAG = "app/router: rule not found: "


class _Profile:
    def __init__(self, profile_id: int, name: str, categories: list[str] | None = None):
        self.id = profile_id
        self.name = name
        self.categories = list(categories or [])
        self.allow_list = []
        self.block_list = []
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
            "inbounds": [{"tag": tag, "protocol": "vless"} for tag in tags],
            "outbounds": list(FILTER_OUTBOUNDS),
        }
        if routing is not None:
            self.config["routing"] = {"rules": list(routing)}
        self.exclude_inbound_tags = []
        self.fallbacks_inbound_tags = []


class _LiveNode:
    def __init__(self, rules: list | None = None):
        self.rules = [dict(row) for row in rules or []]
        self.added: list[str] = []
        self.removed: list[str] = []
        self.refused: list[str] = []
        self.loading: list[dict] | None = None

    def restarts_with(self, rules: list[dict]) -> None:
        self.loading = [dict(row) for row in rules]

    def listing(self) -> list[dict]:
        listed = [
            {"ruleTag": str(row.get("ruleTag") or ""), "outboundTag": str(row.get("outboundTag") or "")}
            for row in self.rules
        ]
        if self.loading is not None:
            self.rules = self.loading
            self.loading = None
        return listed

    async def add_routing_rule(self, rule, should_reset):
        payload = json.loads(rule)
        tag = str(payload.get("ruleTag") or "")
        if tag and tag in self.tags():
            self.refused.append(tag)
            raise NodeAPIError(code=13, detail=f"{DUPLICATE_RULE_TAG}{tag}")
        self.rules.append({**payload, "ruleTag": tag, "outboundTag": payload.get("outboundTag") or ""})
        self.added.append(tag)

    async def remove_routing_rule(self, rule_tag):
        if rule_tag not in self.tags():
            raise NodeAPIError(code=13, detail=f"{MISSING_RULE_TAG}{rule_tag}")
        self.rules = [row for row in self.rules if str(row.get("ruleTag") or "") != rule_tag]
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
            nulls = "core_config_id IS NULL" in str(statement)
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


def _fleet(monkeypatch, cores, nodes, assignments, live=None) -> _DB:
    by_id = {core.id: core for core in cores}

    async def core_of(db, core_id):
        return by_id.get(core_id)

    db = _DB(nodes, assignments)
    for node in nodes:
        db.live[node.id] = _LiveNode((live or {}).get(node.id, []))

    async def live_rules(node_id):
        node = db.live.get(node_id)
        if node is None:
            raise service.EnforcementError(f"node {node_id} is not attached to this panel", code=404)
        return node.listing()

    monkeypatch.setattr(service, "get_core_config_by_id", core_of)
    monkeypatch.setattr(service, "node_manager", _Manager(db))
    monkeypatch.setattr(service, "live_rules", live_rules)
    return db


def _core_writer(monkeypatch, db: _DB, cores: list[_Core], captured: dict):
    import app.operation.core as core_operation

    by_id = {core.id: core for core in cores}

    class _Operation:
        def __init__(self, operator_type=None):
            self.operator_type = operator_type

        async def modify_core(self, db_arg, core_id_arg, payload, admin):
            captured["core_id"] = core_id_arg
            captured["config"] = payload.config
            by_id[core_id_arg].config = payload.config

    async def restart(db_arg, core_id_arg, admin):
        stored = _stored(by_id[core_id_arg].config)
        for node in db.nodes:
            if node.core_config_id == core_id_arg:
                db.live[node.id].restarts_with(stored)

    monkeypatch.setattr(core_operation, "CoreOperation", _Operation)
    monkeypatch.setattr(service, "_restart_core_nodes", restart)


def _stored(config: dict) -> list[dict]:
    return list((config.get("routing") or {}).get("rules") or [])


def _written(config: dict) -> list[dict]:
    return [rule for rule in _stored(config) if owns_tag(str(rule.get("ruleTag") or ""))]


def _shared_core(monkeypatch, routing=None):
    tags = [f"in-{index}" for index in range(3)]
    core = _Core(64, tags, routing=routing)
    nodes = [_Node(640, 64), _Node(650, 64)]
    profile = _Profile(1, "ads", categories=["ads"])
    assignments = [_Assignment(241 + index, profile, tag) for index, tag in enumerate(tags)]
    db = _fleet(monkeypatch, [core], nodes, assignments)
    return db, core, assignments


@pytest.mark.asyncio
async def test_a_shared_core_hands_the_same_rule_tag_to_the_config_and_to_the_live_push(
    monkeypatch: pytest.MonkeyPatch,
):
    db, _, _ = _shared_core(monkeypatch)

    persisted = await service.core_persisted_rules(db, 64)
    plan = await service.core_rule_plan(db, 64, await service._assignments_for_node(db, 650))

    assert [rule["ruleTag"] for rule in persisted] == [rule["ruleTag"] for rule in plan.rules]


@pytest.mark.asyncio
async def test_a_core_that_comes_back_with_its_stored_rules_is_not_told_to_add_them_again(
    monkeypatch: pytest.MonkeyPatch,
):
    db, core, _ = _shared_core(monkeypatch)
    stored = await service.core_persisted_rules(db, 64)
    core.config["routing"] = {"rules": list(stored)}
    db.live[640].restarts_with(stored)

    pushed = await service.push_live(db, 640)

    assert db.live[640].refused == []
    assert db.live[640].tags() == [rule["ruleTag"] for rule in pushed]


@pytest.mark.asyncio
async def test_the_rewrite_that_restarts_a_shared_core_leaves_one_copy_of_every_rule(
    monkeypatch: pytest.MonkeyPatch,
):
    db, core, _ = _shared_core(monkeypatch)
    captured: dict = {}
    _core_writer(monkeypatch, db, [core], captured)

    await service.persist_core_rules(db, 64, admin=object(), allow_restart=True)
    written = [rule["ruleTag"] for rule in _written(captured["config"])]

    for node_id in (640, 650):
        assert db.live[node_id].refused == []
        assert db.live[node_id].tags() == written
        assert len(db.live[node_id].tags()) == len(set(db.live[node_id].tags()))


@pytest.mark.asyncio
async def test_an_apply_over_a_restarting_shared_core_still_reports_the_filter_in_force(
    monkeypatch: pytest.MonkeyPatch,
):
    db, core, assignments = _shared_core(monkeypatch)
    captured: dict = {}
    _core_writer(monkeypatch, db, [core], captured)

    result = await service.apply_assignment(db, assignments[0], admin=object(), allow_restart=True)

    assert result["nodes"] == 2
    assert assignments[0].enforced is True
    assert db.live[640].refused == []
    assert db.live[650].refused == []


@pytest.mark.asyncio
async def test_a_node_that_holds_nothing_still_gets_each_rule_once(monkeypatch: pytest.MonkeyPatch):
    db, _, _ = _shared_core(monkeypatch)

    pushed = await service.push_live(db, 640)

    assert [rule["ruleTag"] for rule in pushed] == db.live[640].added
    assert db.live[640].tags() == db.live[640].added
    assert db.live[640].refused == []
