import json

import pytest
from PasarGuardNodeBridge import NodeAPIError

from app.db.models import CoreType, Node
from app.fork.content_filter import capability, service
from app.fork.content_filter.rules import tag_assignment_id
from app.fork.models.content_filter import ContentFilterSniffingOverride

FILTER_OUTBOUNDS = [{"tag": "BLOCK", "protocol": "blackhole"}, {"tag": "DIRECT", "protocol": "freedom"}]


class _Profile:
    def __init__(self, blocked: str):
        self.categories = []
        self.allow_list = []
        self.block_list = [blocked]
        self.strict_mode = False


class _Assignment:
    def __init__(self, assignment_id: int, inbound_tag: str, node_id: int | None = None, is_enabled: bool = True):
        self.id = assignment_id
        self.inbound_tag = inbound_tag
        self.node_id = node_id
        self.is_enabled = is_enabled
        self.enforced = True
        self.last_error = None
        self.last_checked_at = None
        self.applied_digest = None
        self.profile = _Profile(f"blocked-{assignment_id}.example")


class _Node:
    def __init__(
        self,
        node_id: int,
        core_id: int,
        extra_cores: list[int] | None = None,
        status: str = "connected",
        node_version: str = capability.MIN_NODE_VERSION,
    ):
        self.id = node_id
        self.name = f"node-{node_id}"
        self.core_config_id = core_id
        self.extra_cores = list(extra_cores or [])
        self.status = status
        self.node_version = node_version


class _Core:
    def __init__(self, core_id: int, tags: list, outbounds: list | None = None):
        self.id = core_id
        self.name = f"core-{core_id}"
        self.type = CoreType.xray
        self.config = {
            "inbounds": [
                {"tag": tag, "protocol": protocol}
                for tag, protocol in ((t if isinstance(t, tuple) else (t, "vless")) for t in tags)
            ],
            "outbounds": FILTER_OUTBOUNDS if outbounds is None else outbounds,
        }
        self.exclude_inbound_tags = []
        self.fallbacks_inbound_tags = []


class _LiveNode:
    def __init__(self, rules: list):
        self.rules = [row if isinstance(row, dict) else {"ruleTag": row, "outboundTag": "BLOCK"} for row in rules]
        self.added: list[str] = []
        self.removed: list[str] = []
        self.refuses_add = False

    async def add_routing_rule(self, rule, should_reset):
        if self.refuses_add:
            raise NodeAPIError(code=13, detail="core is gone")
        payload = json.loads(rule)
        tag = payload.get("ruleTag") or ""
        self.rules.append({**payload, "ruleTag": tag, "outboundTag": payload.get("outboundTag") or ""})
        self.added.append(tag)

    async def remove_routing_rule(self, rule_tag):
        self.rules = [row for row in self.rules if row["ruleTag"] != rule_tag]
        self.removed.append(rule_tag)

    async def test_route(self, inbound_tag, network, target_domain, target_port):
        return _Verdict()

    def tags(self) -> list[str]:
        return [row["ruleTag"] for row in self.rules]


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
        self.overrides = []
        self.added = []
        self.deleted = []
        self.commits = 0

    async def execute(self, statement):
        entity = statement.column_descriptions[0].get("entity")
        params = statement.compile().params
        if entity is Node:
            core_id = params.get("core_config_id_1")
            rendered = str(statement)
            widened = "node_additional_cores" in rendered
            nulls = "core_config_id IS NULL" in rendered
            rows = [
                node
                for node in self.nodes
                if core_id is None
                or node.core_config_id == core_id
                or (widened and core_id in node.extra_cores)
                or (nulls and node.core_config_id is None)
            ]
            return _Result(sorted(rows, key=lambda node: node.id))
        if entity is ContentFilterSniffingOverride:
            core_id = params.get("core_id_1")
            return _Result([row for row in self.overrides if row.core_id == core_id])
        if entity is None:
            node = next((row for row in self.nodes if row.id == params.get("node_id_1")), None)
            return _Result(list(node.extra_cores) if node is not None else [])
        node_id = params.get("node_id_1")
        return _Result([row for row in self.assignments if row.is_enabled and row.node_id in (None, node_id)])

    async def get(self, model, pk):
        return next((node for node in self.nodes if node.id == pk), None)

    def add(self, row):
        self.added.append(row)
        self.overrides.append(row)

    async def delete(self, row):
        self.deleted.append(row)
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


def _fleet(monkeypatch, cores, nodes, assignments, live=None, online=None) -> _DB:
    by_id = {core.id: core for core in cores}

    async def core_of(db, core_id):
        return by_id.get(core_id)

    db = _DB(nodes, assignments)
    for node_id in online if online is not None else [node.id for node in nodes]:
        db.live[node_id] = _LiveNode((live or {}).get(node_id, []))

    async def live_rules(node_id):
        node = db.live.get(node_id)
        if node is None:
            raise service.EnforcementError(f"node {node_id} is not attached to this panel", code=404)
        return [
            {"ruleTag": row.get("ruleTag") or "", "outboundTag": row.get("outboundTag") or ""} for row in node.rules
        ]

    monkeypatch.setattr(service, "get_core_config_by_id", core_of)
    monkeypatch.setattr(service, "node_manager", _Manager(db))
    monkeypatch.setattr(service, "live_rules", live_rules)
    return db


def _core_writer(monkeypatch, captured: dict, fails=None):
    import app.operation.core as core_operation

    class _Operation:
        def __init__(self, operator_type=None):
            self.operator_type = operator_type

        async def modify_core(self, db_arg, core_id_arg, payload, admin):
            if fails is not None:
                raise fails
            captured["core_id"] = core_id_arg
            captured["config"] = payload.config

    async def restart(db_arg, core_id_arg, admin):
        captured.setdefault("restarted", []).append(core_id_arg)

    monkeypatch.setattr(core_operation, "CoreOperation", _Operation)
    monkeypatch.setattr(service, "_restart_core_nodes", restart)


async def _persist(monkeypatch, db, core_id, allow_restart: bool = True) -> dict:
    captured: dict = {}
    _core_writer(monkeypatch, captured)
    captured["changed"] = await service.persist_core_rules(db, core_id, admin=object(), allow_restart=allow_restart)
    return captured


def _owners(rules: list[dict]) -> set[int]:
    return {tag_assignment_id(str(rule.get("ruleTag") or "")) for rule in rules}


def _written_owners(config: dict) -> set[int]:
    return _owners(config.get("routing", {}).get("rules") or [])


def _sniffing(config: dict, tag: str):
    for inbound in config.get("inbounds") or []:
        if inbound.get("tag") == tag:
            return inbound.get("sniffing")
    return None


@pytest.mark.asyncio
async def test_a_pin_never_lands_in_a_core_that_other_nodes_also_run(monkeypatch: pytest.MonkeyPatch):
    core = _Core(1, ["in-a", "in-b"])
    assignment = _Assignment(7, "in-a", node_id=10)
    db = _fleet(monkeypatch, [core], [_Node(10, 1), _Node(11, 1)], [assignment])

    assert await service.core_persisted_rules(db, 1) == []

    captured = await _persist(monkeypatch, db, 1)
    assert 7 not in _written_owners(captured["config"])
    assert await service.assignment_delivery(db, assignment) == "live"


@pytest.mark.asyncio
async def test_a_live_only_pin_still_gets_sniffing_on_its_endpoint(monkeypatch: pytest.MonkeyPatch):
    core = _Core(1, ["in-a", "in-b"])
    db = _fleet(monkeypatch, [core], [_Node(10, 1), _Node(11, 1)], [_Assignment(7, "in-a", node_id=10)])

    captured = await _persist(monkeypatch, db, 1)

    assert _sniffing(captured["config"], "in-a") == service.SNIFFING
    assert _sniffing(captured["config"], "in-b") is None
    assert {row.inbound_tag for row in db.added} == {"in-a"}


@pytest.mark.asyncio
async def test_a_pin_on_a_core_carrying_only_that_node_is_written(monkeypatch: pytest.MonkeyPatch):
    core = _Core(2, ["in-a"])
    assignment = _Assignment(8, "in-a", node_id=20)
    db = _fleet(monkeypatch, [core], [_Node(20, 2)], [assignment])

    assert _owners(await service.core_persisted_rules(db, 2)) == {8}

    captured = await _persist(monkeypatch, db, 2)
    assert 8 in _written_owners(captured["config"])
    assert await service.assignment_delivery(db, assignment) == "core"


@pytest.mark.asyncio
async def test_a_floating_assignment_on_every_node_of_the_core_is_written(monkeypatch: pytest.MonkeyPatch):
    core = _Core(3, ["in-a", "in-b"])
    assignment = _Assignment(9, "in-a")
    db = _fleet(monkeypatch, [core], [_Node(30, 3), _Node(31, 3)], [assignment])

    assert _owners(await service.core_persisted_rules(db, 3)) == {9}

    captured = await _persist(monkeypatch, db, 3)
    assert 9 in _written_owners(captured["config"])
    assert await service.assignment_delivery(db, assignment) == "core"


@pytest.mark.asyncio
async def test_a_floating_assignment_missing_from_one_node_is_not_written(monkeypatch: pytest.MonkeyPatch):
    core = _Core(4, ["in-a", "in-b"])
    assignment = _Assignment(11, "in-a")
    db = _fleet(monkeypatch, [core], [_Node(40, 4), _Node(41, 4)], [assignment])

    async def tags(db_arg, node_id):
        return {"in-a", "in-b"} if node_id == 40 else {"in-b"}

    monkeypatch.setattr(service, "node_inbound_tags", tags)

    assert await service.core_persisted_rules(db, 4) == []

    captured = await _persist(monkeypatch, db, 4)
    assert 11 not in _written_owners(captured["config"])
    assert await service.assignment_delivery(db, assignment) == "live"


@pytest.mark.asyncio
async def test_a_core_keeps_the_rules_of_the_assignments_that_reach_all_of_its_nodes(monkeypatch: pytest.MonkeyPatch):
    core = _Core(5, ["in-a", "in-b"])
    pinned = _Assignment(12, "in-a", node_id=50)
    floating = _Assignment(13, "in-b")
    db = _fleet(monkeypatch, [core], [_Node(50, 5), _Node(51, 5)], [pinned, floating])

    captured = await _persist(monkeypatch, db, 5)

    assert _written_owners(captured["config"]) == {13}
    assert await service.assignment_delivery(db, pinned) == "live"
    assert await service.assignment_delivery(db, floating) == "core"
    assert _sniffing(captured["config"], "in-a") == service.SNIFFING
    assert _sniffing(captured["config"], "in-b") == service.SNIFFING


@pytest.mark.asyncio
async def test_a_live_only_pin_still_reaches_the_node_it_targets(monkeypatch: pytest.MonkeyPatch):
    core = _Core(6, ["in-a", "in-b"])
    db = _fleet(monkeypatch, [core], [_Node(60, 6), _Node(61, 6)], [_Assignment(14, "in-a", node_id=60)])

    pushed = await service.push_live(db, 60)
    assert _owners(pushed) == {14}
    assert db.live[60].added == [pushed[0]["ruleTag"]]
    assert pushed[0]["inboundTag"] == ["in-a"]

    assert await service.push_live(db, 61) == []
    assert db.live[61].added == []


@pytest.mark.asyncio
async def test_a_disabled_assignment_is_never_reported_as_persisted(monkeypatch: pytest.MonkeyPatch):
    core = _Core(8, ["in-a"])
    assignment = _Assignment(16, "in-a", node_id=80, is_enabled=False)
    db = _fleet(monkeypatch, [core], [_Node(80, 8)], [assignment])

    assert await service.core_persisted_rules(db, 8) == []
    assert await service.assignment_delivery(db, assignment) == "live"


@pytest.mark.asyncio
async def test_a_withdrawn_filter_is_stripped_from_every_node_on_the_core(monkeypatch: pytest.MonkeyPatch):
    core = _Core(1, ["Shadowsocks TCP", "Shadowsocks Open"])
    assignment = _Assignment(1, "Shadowsocks TCP", node_id=5)
    stale = service.assignment_rules(assignment)[0]["ruleTag"]
    core.config["routing"] = {
        "rules": [
            {
                "type": "field",
                "ruleTag": stale,
                "inboundTag": ["Shadowsocks TCP"],
                "domain": ["domain:leak-probe.test"],
                "outboundTag": "BLOCK",
            }
        ]
    }
    db = _fleet(monkeypatch, [core], [_Node(5, 1), _Node(6, 1)], [assignment], live={5: [stale], 6: [stale]})

    written: dict = {}
    _core_writer(monkeypatch, written)

    await service.withdraw_assignment(db, assignment, admin=object(), allow_restart=True)

    assert _written_owners(written["config"]) == set()
    assert db.live[5].tags() == []
    assert db.live[6].tags() == []
    assert db.live[6].removed == [stale]
    assert db.live[6].added == []
    assert db.assignments == []


@pytest.mark.asyncio
async def test_a_core_rewrite_reports_the_nodes_it_could_not_refresh(monkeypatch: pytest.MonkeyPatch):
    core = _Core(9, ["in-a"])
    nodes = [_Node(90, 9), _Node(91, 9), _Node(92, 9, status="disabled")]
    db = _fleet(
        monkeypatch,
        [core],
        nodes,
        [_Assignment(17, "in-a")],
        live={90: ["pgcf-99-block"], 92: ["pgcf-99-block"]},
        online=[90, 92],
    )

    problems = await service.refresh_core_nodes(db, 9)

    assert problems == ["node 91: node 91 is not attached to this panel"]
    assert _owners(db.live[90].rules) == {17}
    assert db.live[92].tags() == ["pgcf-99-block"]


@pytest.mark.asyncio
async def test_a_node_attached_through_additional_cores_counts_on_that_core(monkeypatch: pytest.MonkeyPatch):
    shared = _Core(3, ["Shadowsocks TCP", "Shadowsocks Open"])
    other = _Core(9, ["in-own"])
    assignment = _Assignment(21, "Shadowsocks TCP", node_id=41)
    db = _fleet(
        monkeypatch,
        [shared, other],
        [_Node(41, 3), _Node(68, 9, extra_cores=[3]), _Node(71, 9, extra_cores=[3])],
        [assignment],
    )

    assert [node.id for node in await service._nodes_on_core(db, 3)] == [41, 68, 71]
    assert await service.core_persisted_rules(db, 3) == []

    captured = await _persist(monkeypatch, db, 3)
    assert 21 not in _written_owners(captured["config"])
    assert await service.assignment_delivery(db, assignment) == "live"


@pytest.mark.asyncio
async def test_an_endpoint_of_an_additional_core_belongs_to_the_node(monkeypatch: pytest.MonkeyPatch):
    shared = _Core(3, ["Shadowsocks TCP", "Shadowsocks Open"])
    other = _Core(9, ["in-own"])
    floating = _Assignment(22, "Shadowsocks Open")
    db = _fleet(
        monkeypatch,
        [shared, other],
        [_Node(41, 3), _Node(68, 9, extra_cores=[3]), _Node(71, 9, extra_cores=[3])],
        [floating],
    )

    assert await service.node_inbound_tags(db, 68) == {"in-own", "Shadowsocks TCP", "Shadowsocks Open"}
    assert sorted(await service.nodes_for_assignment(db, floating)) == [41, 68, 71]
    assert _owners(await service.core_persisted_rules(db, 3)) == {22}
    assert await service.assignment_delivery(db, floating) == "core"


@pytest.mark.asyncio
async def test_a_whole_node_filter_only_scopes_the_endpoints_it_can_route(monkeypatch: pytest.MonkeyPatch):
    core = _Core(7, ["in-a", "in-b", ("relay-in", "dokodemo-door")])
    assignment = _Assignment(30, "", node_id=70)
    db = _fleet(monkeypatch, [core], [_Node(70, 7), _Node(71, 7), _Node(72, 7)], [assignment])

    rules = await service.rules_for_node(db, assignment, 70)

    assert rules
    for rule in rules:
        assert rule["inboundTag"] == ["in-a", "in-b"]
    assert await service.core_persisted_rules(db, 7) == []


@pytest.mark.asyncio
async def test_a_whole_node_filter_leaves_the_tunnel_endpoints_alone(monkeypatch: pytest.MonkeyPatch):
    core = _Core(7, ["in-a", "in-b", ("relay-in", "dokodemo-door")])
    db = _fleet(monkeypatch, [core], [_Node(70, 7), _Node(71, 7), _Node(72, 7)], [_Assignment(30, "", node_id=70)])

    captured = await _persist(monkeypatch, db, 7)

    assert _sniffing(captured["config"], "relay-in") is None
    assert _sniffing(captured["config"], "in-a") == service.SNIFFING
    assert _sniffing(captured["config"], "in-b") == service.SNIFFING
    assert {row.inbound_tag for row in db.added} == {"in-a", "in-b"}


@pytest.mark.asyncio
async def test_a_reload_is_refused_until_the_caller_authorises_it(monkeypatch: pytest.MonkeyPatch):
    core = _Core(7, ["in-a", "in-b", ("relay-in", "dokodemo-door")])
    db = _fleet(monkeypatch, [core], [_Node(70, 7), _Node(71, 7), _Node(72, 7)], [_Assignment(30, "", node_id=70)])

    captured: dict = {}
    _core_writer(monkeypatch, captured)

    with pytest.raises(service.ReloadRequired) as refused:
        await service.persist_core_rules(db, 7, admin=object())

    assert refused.value.code == 409
    assert refused.value.node_ids == [70, 71, 72]
    assert refused.value.inbound_tags == ["in-a", "in-b"]
    assert "config" not in captured
    assert db.added == []


@pytest.mark.asyncio
async def test_an_authorised_reload_is_the_only_thing_that_restarts_the_fleet(monkeypatch: pytest.MonkeyPatch):
    core = _Core(7, ["in-a"])
    db = _fleet(monkeypatch, [core], [_Node(70, 7)], [_Assignment(31, "in-a", node_id=70)])

    captured = await _persist(monkeypatch, db, 7, allow_restart=True)
    assert captured["restarted"] == [7]

    core.config = captured["config"]
    again = await _persist(monkeypatch, db, 7, allow_restart=False)
    assert again["changed"] is False
    assert "restarted" not in again


@pytest.mark.asyncio
async def test_a_renamed_endpoint_keeps_the_setting_the_operator_had(monkeypatch: pytest.MonkeyPatch):
    core = _Core(8, ["in-a"])
    core.config["inbounds"][0]["sniffing"] = {"enabled": False}
    assignment = _Assignment(32, "in-a", node_id=80)
    db = _fleet(monkeypatch, [core], [_Node(80, 8)], [assignment])

    captured = await _persist(monkeypatch, db, 8)
    core.config = captured["config"]
    assert len(db.overrides) == 1

    core.config["inbounds"][0]["tag"] = "in-a-v2"
    assignment.is_enabled = False
    await _persist(monkeypatch, db, 8)

    assert len(db.overrides) == 1
    assert db.overrides[0].inbound_tag == "in-a"
    assert service._ledger_parts(db.overrides[0])[0] == {"enabled": False}

    plan = await service.plan_sniffing_overrides(db, 8, core.config, set())
    assert plan.delete == []
    assert any("in-a" in problem for problem in plan.problems)


@pytest.mark.asyncio
async def test_sniffing_the_operator_changed_is_never_taken_back(monkeypatch: pytest.MonkeyPatch):
    core = _Core(9, ["in-a"])
    assignment = _Assignment(33, "in-a", node_id=90)
    db = _fleet(monkeypatch, [core], [_Node(90, 9)], [assignment])

    captured = await _persist(monkeypatch, db, 9)
    core.config = captured["config"]

    operator_choice = {"enabled": True, "destOverride": ["fakedns"]}
    core.config["inbounds"][0]["sniffing"] = dict(operator_choice)

    written: dict = {}
    _core_writer(monkeypatch, written)
    await service.withdraw_assignment(db, assignment, admin=object(), allow_restart=True)

    assert _sniffing(written["config"], "in-a") == operator_choice
    assert db.overrides == []


@pytest.mark.asyncio
async def test_a_node_that_dies_after_the_check_is_named_instead_of_hidden(monkeypatch: pytest.MonkeyPatch):
    core = _Core(10, ["in-a"])
    assignment = _Assignment(40, "in-a")
    db = _fleet(monkeypatch, [core], [_Node(100, 10), _Node(101, 10)], [assignment])
    db.live[101].refuses_add = True

    captured: dict = {}
    _core_writer(monkeypatch, captured)

    with pytest.raises(service.EnforcementError) as failed:
        await service.apply_assignment(db, assignment, admin=object(), allow_restart=True)

    assert failed.value.code == 502
    assert "node 101" in failed.value.detail
    assert "in force on 1 of 2 nodes" in failed.value.detail
    assert assignment.enforced is False
    assert "node 101" in assignment.last_error
    assert _written_owners(captured["config"]) == {40}
    assert _owners(db.live[100].rules) == {40}


@pytest.mark.asyncio
async def test_a_withdrawal_keeps_the_row_until_the_node_is_clean(monkeypatch: pytest.MonkeyPatch):
    core = _Core(11, ["in-a"])
    assignment = _Assignment(41, "in-a", node_id=110)
    db = _fleet(monkeypatch, [core], [_Node(110, 11)], [assignment], online=[])

    written: dict = {}
    _core_writer(monkeypatch, written)

    with pytest.raises(service.EnforcementError) as failed:
        await service.withdraw_assignment(db, assignment, admin=object(), allow_restart=True)

    assert failed.value.code == 502
    assert "node 110" in failed.value.detail
    assert db.assignments == [assignment]
    assert assignment.is_enabled is False


@pytest.mark.asyncio
async def test_a_core_that_refuses_the_rewrite_is_not_a_crash(monkeypatch: pytest.MonkeyPatch):
    core = _Core(12, ["in-a"])
    assignment = _Assignment(42, "in-a", node_id=120)
    db = _fleet(monkeypatch, [core], [_Node(120, 12)], [assignment])

    _core_writer(monkeypatch, {}, fails=ValueError("core refused"))

    with pytest.raises(service.EnforcementError) as failed:
        await service.withdraw_assignment(db, assignment, admin=object(), allow_restart=True)

    assert "core refused" in failed.value.detail
    assert db.assignments == [assignment]


@pytest.mark.asyncio
async def test_a_fleet_wide_filter_is_not_called_enforced_by_one_healthy_node(monkeypatch: pytest.MonkeyPatch):
    core = _Core(13, ["in-a"])
    assignment = _Assignment(50, "in-a")
    assignment.enforced = False
    db = _fleet(monkeypatch, [core], [_Node(130, 13), _Node(131, 13)], [assignment], online=[130])

    error = await service.reconcile_node(db, 130)

    assert assignment.enforced is False
    assert "node 131" in assignment.last_error
    assert error is not None
    assert _owners(db.live[130].rules) == {50}


@pytest.mark.asyncio
async def test_a_core_that_names_its_outbounds_differently_is_pointed_at_the_real_ones(
    monkeypatch: pytest.MonkeyPatch,
):
    core = _Core(
        14,
        ["in-a"],
        outbounds=[{"tag": "blocked", "protocol": "blackhole"}, {"tag": "out", "protocol": "freedom"}],
    )
    assignment = _Assignment(51, "in-a", node_id=140)
    db = _fleet(monkeypatch, [core], [_Node(140, 14)], [assignment])

    pushed = await service.push_live(db, 140)
    assert {rule["outboundTag"] for rule in pushed} == {"blocked"}

    captured = await _persist(monkeypatch, db, 14)
    written = [rule for rule in captured["config"]["routing"]["rules"] if service.owns_tag(rule.get("ruleTag"))]
    assert {rule["outboundTag"] for rule in written} == {"blocked"}


@pytest.mark.asyncio
async def test_a_core_with_nothing_to_block_with_is_refused_on_the_live_path(monkeypatch: pytest.MonkeyPatch):
    core = _Core(15, ["in-a"], outbounds=[{"tag": "out", "protocol": "freedom"}])
    assignment = _Assignment(52, "in-a", node_id=150)
    db = _fleet(monkeypatch, [core], [_Node(150, 15)], [assignment])

    with pytest.raises(service.EnforcementError) as refused:
        await service.push_live(db, 150)

    assert refused.value.code == 409
    assert "BLOCK" in refused.value.detail
    assert "out" in refused.value.detail
    assert db.live[150].added == []


@pytest.mark.asyncio
async def test_a_node_attached_through_an_extra_core_has_that_core_rebuilt(monkeypatch: pytest.MonkeyPatch):
    shared = _Core(3, ["Shadowsocks Open"])
    own = _Core(9, ["in-own"])
    db = _fleet(monkeypatch, [shared, own], [_Node(68, 9, extra_cores=[3])], [])

    assert await service._cores_of_nodes(db, [68]) == [9, 3]


@pytest.mark.asyncio
async def test_a_withdrawal_cleans_the_core_of_a_disabled_node(monkeypatch: pytest.MonkeyPatch):
    core = _Core(16, ["in-a"])
    assignment = _Assignment(53, "in-a", node_id=160)
    stale = service.assignment_rules(assignment)[0]["ruleTag"]
    core.config["routing"] = {"rules": [{"ruleTag": stale, "outboundTag": "BLOCK", "inboundTag": ["in-a"]}]}
    db = _fleet(monkeypatch, [core], [_Node(160, 16, status="disabled")], [assignment])

    assert await service.nodes_for_assignment(db, assignment) == []
    assert await service.cores_of_assignment(db, assignment) == [16]

    written: dict = {}
    _core_writer(monkeypatch, written)
    await service.withdraw_assignment(db, assignment, admin=object(), allow_restart=True)

    assert _written_owners(written["config"]) == set()
    assert db.assignments == []


@pytest.mark.asyncio
async def test_a_node_without_a_core_of_its_own_belongs_to_the_default_core(monkeypatch: pytest.MonkeyPatch):
    core = _Core(1, ["in-a"])
    orphan = _Node(170, 1)
    orphan.core_config_id = None
    assignment = _Assignment(54, "in-a", node_id=170)
    db = _fleet(monkeypatch, [core], [orphan], [assignment])

    assert service.core_id_of(orphan) == service.DEFAULT_CORE_ID
    assert [node.id for node in await service._nodes_on_core(db, 1)] == [170]
    assert await service.cores_of_assignment(db, assignment) == [1]
    assert await service._cores_of_nodes(db, [170]) == [1]


@pytest.mark.asyncio
async def test_an_edited_profile_takes_its_old_rules_off_the_node(monkeypatch: pytest.MonkeyPatch):
    core = _Core(17, ["in-a"])
    assignment = _Assignment(55, "in-a", node_id=180)
    db = _fleet(monkeypatch, [core], [_Node(180, 17)], [assignment])

    first = await service.push_live(db, 180)
    old_tag = first[0]["ruleTag"]
    assert db.live[180].tags() == [old_tag]

    assignment.profile.block_list = ["another-domain.example"]
    second = await service.push_live(db, 180)
    new_tag = second[0]["ruleTag"]

    assert new_tag != old_tag
    assert db.live[180].tags() == [new_tag]
    assert old_tag in db.live[180].removed


@pytest.mark.asyncio
async def test_a_profile_the_rule_builder_rejects_is_an_answer_not_a_crash(monkeypatch: pytest.MonkeyPatch):
    core = _Core(18, ["in-a"])
    assignment = _Assignment(56, "in-a", node_id=190)
    assignment.profile.block_list = ["domain:example.com"]
    db = _fleet(monkeypatch, [core], [_Node(190, 18)], [assignment])

    with pytest.raises(service.EnforcementError) as refused:
        await service.rules_for_node(db, assignment, 190)

    assert refused.value.code == 422
    assert "domain:example.com" in refused.value.detail

    error = await service.reconcile_node(db, 190)
    assert error is not None
    assert "domain:example.com" in assignment.last_error
    assert assignment.enforced is False


@pytest.mark.asyncio
async def test_the_stored_order_is_the_order_the_node_ends_up_with(monkeypatch: pytest.MonkeyPatch):
    core = _Core(19, ["in-a"])
    core.config["routing"] = {"rules": [{"ruleTag": "op-1", "outboundTag": "proxy", "inboundTag": ["other"]}]}
    assignment = _Assignment(57, "in-a", node_id=200)
    db = _fleet(monkeypatch, [core], [_Node(200, 19)], [assignment], live={200: ["op-1"]})

    captured = await _persist(monkeypatch, db, 19)
    stored = [rule.get("ruleTag") for rule in captured["config"]["routing"]["rules"]]

    assert stored[0] == "op-1"
    assert service.owns_tag(stored[1])
    assert db.live[200].tags() == stored


class _Refusing:
    async def list_routing_rules(self):
        raise NodeAPIError(code=501, detail="unknown service RoutingService")


class _RefusingManager:
    async def get_node(self, node_id):
        return _Refusing()


@pytest.mark.asyncio
async def test_a_node_whose_config_declares_the_service_is_told_to_reconnect(monkeypatch: pytest.MonkeyPatch):
    async def declared(node_id):
        return 7, True

    monkeypatch.setattr(service, "node_manager", _RefusingManager())
    monkeypatch.setattr(service, "_main_core_routing", declared)

    with pytest.raises(service.EnforcementError) as refused:
        await service.live_rules(71)

    assert refused.value.code == 409
    assert "reconnect or restart the node" in refused.value.detail
    assert "api.services" not in refused.value.detail


@pytest.mark.asyncio
async def test_a_node_whose_config_lacks_the_service_is_told_to_add_it(monkeypatch: pytest.MonkeyPatch):
    async def declared(node_id):
        return 7, False

    monkeypatch.setattr(service, "node_manager", _RefusingManager())
    monkeypatch.setattr(service, "_main_core_routing", declared)

    with pytest.raises(service.EnforcementError) as refused:
        await service.live_rules(30)

    assert 'Add "RoutingService" to api.services in core 7' in refused.value.detail
    assert "reconnect or restart the node" not in refused.value.detail


@pytest.mark.asyncio
async def test_an_unreadable_config_makes_no_claim_about_the_cause(monkeypatch: pytest.MonkeyPatch):
    async def declared(node_id):
        return None, None

    monkeypatch.setattr(service, "node_manager", _RefusingManager())
    monkeypatch.setattr(service, "_main_core_routing", declared)

    with pytest.raises(service.EnforcementError) as refused:
        await service.live_rules(30)

    assert "Check whether the core config stored for it lists RoutingService" in refused.value.detail


@pytest.mark.asyncio
async def test_sniffing_the_operator_already_set_is_left_exactly_as_it_is(monkeypatch: pytest.MonkeyPatch):
    core = _Core(12, ["in-a"])
    operator_choice = {"enabled": True, "destOverride": ["http", "tls", "quic"], "routeOnly": True}
    core.config["inbounds"][0]["sniffing"] = dict(operator_choice)
    assignment = _Assignment(60, "in-a", node_id=120)
    db = _fleet(monkeypatch, [core], [_Node(120, 12)], [assignment])

    captured = await _persist(monkeypatch, db, 12, allow_restart=False)

    assert captured["changed"] is True
    assert "restarted" not in captured
    assert _sniffing(captured["config"], "in-a") == operator_choice
    assert db.added == []
    assert 60 in _written_owners(captured["config"])


def test_a_rule_left_over_from_an_earlier_edit_counts_as_stale():
    assignment = _Assignment(70, "in-a", node_id=1)
    old = service.assignment_rules(assignment)[0]["ruleTag"]
    assignment.profile.block_list = ["later-domain.example"]
    current = service.assignment_rules(assignment)[0]["ruleTag"]
    live = [{"ruleTag": old}, {"ruleTag": current}, {"ruleTag": "pgcf-70-block"}, {"ruleTag": "op-1"}]

    assert old != current
    assert service.stale_owned_tags(live, {70}) == ["pgcf-70-block"]
    assert service.stale_owned_tags(live, {70}, wanted_tags={current}) == [old, "pgcf-70-block"]


@pytest.mark.asyncio
async def test_an_endpoint_rule_is_never_written_into_a_core_without_that_endpoint(monkeypatch: pytest.MonkeyPatch):
    own = _Core(9, ["in-own"])
    shared = _Core(3, ["Shadowsocks Open"])
    floating = _Assignment(80, "Shadowsocks Open")
    db = _fleet(monkeypatch, [own, shared], [_Node(68, 9, extra_cores=[3])], [floating])

    assert {row.id for row in await service._core_persisted_assignments(db, 9)} == {80}
    assert await service.core_persisted_rules(db, 9) == []
    assert _owners(await service.core_persisted_rules(db, 3)) == {80}

    captured = await _persist(monkeypatch, db, 9)
    assert _written_owners(captured["config"]) == set()


@pytest.mark.asyncio
async def test_a_whole_node_filter_is_scoped_to_the_core_being_written(monkeypatch: pytest.MonkeyPatch):
    primary = _Core(7, ["in-a", "in-b", ("relay-in", "dokodemo-door")])
    extra = _Core(20, ["edge-in"])
    assignment = _Assignment(81, "", node_id=70)
    db = _fleet(monkeypatch, [primary, extra], [_Node(70, 7, extra_cores=[20])], [assignment])

    on_primary = await service.rules_in_core(db, assignment, 7)
    on_extra = await service.rules_in_core(db, assignment, 20)
    live = await service.rules_for_node(db, assignment, 70)

    assert [rule["inboundTag"] for rule in on_primary] == [["in-a", "in-b"]]
    assert [rule["inboundTag"] for rule in on_extra] == [["edge-in"]]
    assert live == on_primary + on_extra
    assert on_extra[0]["ruleTag"] != on_primary[0]["ruleTag"]
    assert await service.unreachable_cores(db, assignment, 70) == [20]


@pytest.mark.asyncio
async def test_a_core_that_no_longer_carries_the_endpoint_drops_the_rule(monkeypatch: pytest.MonkeyPatch):
    core = _Core(21, ["in-a"])
    assignment = _Assignment(82, "in-a", node_id=210)
    db = _fleet(monkeypatch, [core], [_Node(210, 21)], [assignment])

    assert _owners(await service.core_persisted_rules(db, 21)) == {82}

    core.config["inbounds"][0]["tag"] = "in-a-v2"
    assert await service.core_persisted_rules(db, 21) == []


@pytest.mark.asyncio
async def test_a_core_rule_that_would_carry_the_same_traffic_stops_the_rewrite(monkeypatch: pytest.MonkeyPatch):
    core = _Core(22, ["in-a"])
    core.config["routing"] = {"rules": [{"ruleTag": "op-all", "outboundTag": "proxy"}]}
    assignment = _Assignment(83, "in-a", node_id=220)
    db = _fleet(monkeypatch, [core], [_Node(220, 22)], [assignment])

    captured: dict = {}
    _core_writer(monkeypatch, captured)

    with pytest.raises(service.EnforcementError) as refused:
        await service.persist_core_rules(db, 22, admin=object(), allow_restart=True)

    assert refused.value.code == 409
    assert "op-all" in refused.value.detail
    assert "config" not in captured


@pytest.mark.asyncio
async def test_a_core_rule_on_another_endpoint_does_not_stop_the_rewrite(monkeypatch: pytest.MonkeyPatch):
    core = _Core(23, ["in-a", "in-b"])
    core.config["routing"] = {"rules": [{"ruleTag": "op-b", "outboundTag": "proxy", "inboundTag": ["in-b"]}]}
    assignment = _Assignment(84, "in-a", node_id=230)
    db = _fleet(monkeypatch, [core], [_Node(230, 23)], [assignment])

    captured = await _persist(monkeypatch, db, 23)

    stored = [rule.get("ruleTag") for rule in captured["config"]["routing"]["rules"]]
    assert stored[0] == "op-b"
    assert 84 in _written_owners(captured["config"])


PROD_CORE_RULE = {
    "type": "field",
    "outboundTag": "DIRECT",
    "domain": ["keyword:instagram", "domain:fbcdn.net", "domain:wikipedia.org"],
}


@pytest.mark.asyncio
async def test_a_core_rule_about_other_domains_does_not_block_the_filter(monkeypatch: pytest.MonkeyPatch):
    core = _Core(24, ["spof 401"])
    core.config["routing"] = {"rules": [{**PROD_CORE_RULE, "domain": ["domain:fbcdn.net", "domain:wikipedia.org"]}]}
    assignment = _Assignment(85, "spof 401", node_id=240)
    assignment.profile.block_list = ["adult.example"]
    db = _fleet(
        monkeypatch,
        [core],
        [_Node(240, 24)],
        [assignment],
        live={240: [{"ruleTag": "", "outboundTag": "DIRECT"}]},
    )

    pushed = await service.push_live(db, 240)
    assert _owners(pushed) == {85}

    captured = await _persist(monkeypatch, db, 24)
    assert 85 in _written_owners(captured["config"])


@pytest.mark.asyncio
async def test_a_keyword_rule_in_the_core_is_an_advisory_not_a_refusal(monkeypatch: pytest.MonkeyPatch):
    core = _Core(24, ["spof 401"])
    core.config["routing"] = {"rules": [dict(PROD_CORE_RULE)]}
    assignment = _Assignment(94, "spof 401", node_id=240)
    assignment.profile.block_list = ["adult.example"]
    db = _fleet(
        monkeypatch,
        [core],
        [_Node(240, 24)],
        [assignment],
        live={240: [{"ruleTag": "", "outboundTag": "DIRECT"}]},
    )

    _core_writer(monkeypatch, {})
    result = await service.apply_assignment(db, assignment, admin=object(), allow_restart=True)

    assert assignment.enforced is True
    assert assignment.last_error is None
    assert result["advisories"]
    assert any("keyword:instagram" in notice for notice in result["advisories"])
    assert result["advisory_note"] == service.ADVISORY_NOTE


@pytest.mark.asyncio
async def test_an_outbound_named_block_that_forwards_is_not_used(monkeypatch: pytest.MonkeyPatch):
    core = _Core(
        25,
        ["in-a"],
        outbounds=[
            {"tag": "BLOCK", "protocol": "freedom"},
            {"tag": "drop", "protocol": "blackhole"},
            {"tag": "DIRECT", "protocol": "freedom"},
        ],
    )
    assignment = _Assignment(87, "in-a", node_id=250)
    db = _fleet(monkeypatch, [core], [_Node(250, 25)], [assignment])

    pushed = await service.push_live(db, 250)
    assert {rule["outboundTag"] for rule in pushed} == {"drop"}

    captured = await _persist(monkeypatch, db, 25)
    written = [rule for rule in captured["config"]["routing"]["rules"] if service.owns_tag(rule.get("ruleTag"))]
    assert {rule["outboundTag"] for rule in written} == {"drop"}


@pytest.mark.asyncio
async def test_a_core_whose_block_outbound_only_forwards_is_refused(monkeypatch: pytest.MonkeyPatch):
    core = _Core(
        26, ["in-a"], outbounds=[{"tag": "BLOCK", "protocol": "freedom"}, {"tag": "DIRECT", "protocol": "freedom"}]
    )
    assignment = _Assignment(88, "in-a", node_id=260)
    db = _fleet(monkeypatch, [core], [_Node(260, 26)], [assignment])

    with pytest.raises(service.EnforcementError) as refused:
        await service.push_live(db, 260)

    assert refused.value.code == 409
    assert "blackhole" in refused.value.detail
    assert "BLOCK" in refused.value.detail
    assert db.live[260].added == []


@pytest.mark.asyncio
async def test_a_core_that_is_not_xray_is_left_out_of_the_filter(monkeypatch: pytest.MonkeyPatch):
    primary = _Core(27, ["in-a"])
    tunnel = _Core(28, ["wg-in"])
    tunnel.type = CoreType.wg
    assignment = _Assignment(89, "", node_id=270)
    db = _fleet(monkeypatch, [primary, tunnel], [_Node(270, 27, extra_cores=[28])], [assignment])

    assert await service.rules_in_core(db, assignment, 28) == []
    assert await service.unreachable_cores(db, assignment, 270) == []
    assert [rule["inboundTag"] for rule in await service.rules_for_node(db, assignment, 270)] == [["in-a"]]


@pytest.mark.asyncio
async def test_a_second_xray_core_on_a_node_is_reported_not_quietly_skipped(monkeypatch: pytest.MonkeyPatch):
    primary = _Core(29, ["in-a"])
    extra = _Core(30, ["edge-in"])
    assignment = _Assignment(90, "", node_id=290)
    db = _fleet(monkeypatch, [primary, extra], [_Node(290, 29, extra_cores=[30])], [assignment])

    assert await service.unreachable_cores(db, assignment, 290) == [30]

    _core_writer(monkeypatch, {})
    with pytest.raises(service.EnforcementError) as reported:
        await service.apply_assignment(db, assignment, admin=object(), allow_restart=True)

    assert "core 30" in reported.value.detail
    assert assignment.enforced is False
    assert "core 30" in assignment.last_error
    assert {rule["inboundTag"][0] for rule in db.live[290].rules if rule.get("inboundTag")} == {"in-a"}


@pytest.mark.asyncio
async def test_a_fleet_wide_filter_ignores_the_disabled_members_of_the_core(monkeypatch: pytest.MonkeyPatch):
    core = _Core(1, ["spof 401"])
    live_ids = [10, 20, 21, 23, 27, 30, 40, 47, 52, 53, 56, 57, 58, 64, 65, 68]
    nodes = [_Node(node_id, 1) for node_id in live_ids]
    nodes += [_Node(16, 1, status="disabled"), _Node(24, 1, status="disabled")]
    assignment = _Assignment(91, "spof 401")
    db = _fleet(monkeypatch, [core], nodes, [assignment], online=live_ids)

    assert sorted(await service.nodes_for_assignment(db, assignment)) == sorted(live_ids)

    _core_writer(monkeypatch, {})
    result = await service.apply_assignment(db, assignment, admin=object(), allow_restart=True)

    assert result["nodes"] == len(live_ids)
    assert assignment.enforced is True
    assert assignment.last_error is None
    assert all(_owners(db.live[node_id].rules) == {91} for node_id in live_ids)


@pytest.mark.asyncio
async def test_a_core_that_carries_none_of_the_rules_does_not_make_a_filter_durable(monkeypatch: pytest.MonkeyPatch):
    shared = _Core(31, ["in-a"])
    private = _Core(32, ["edge-in"])
    assignment = _Assignment(92, "in-a", node_id=300)
    db = _fleet(
        monkeypatch,
        [shared, private],
        [_Node(300, 31, extra_cores=[32]), _Node(301, 31)],
        [assignment],
    )

    assert {row.id for row in await service._core_persisted_assignments(db, 32)} == {92}
    assert await service.rules_in_core(db, assignment, 32) == []
    assert await service.core_persisted_rules(db, 31) == []
    assert await service.assignment_delivery(db, assignment) == "live"


@pytest.mark.asyncio
async def test_a_filter_on_a_core_of_its_own_is_still_reported_as_durable(monkeypatch: pytest.MonkeyPatch):
    own = _Core(33, ["in-a"])
    private = _Core(34, ["edge-in"])
    assignment = _Assignment(93, "in-a", node_id=310)
    db = _fleet(monkeypatch, [own, private], [_Node(310, 33, extra_cores=[34])], [assignment])

    assert _owners(await service.core_persisted_rules(db, 33)) == {93}
    assert await service.rules_in_core(db, assignment, 34) == []
    assert await service.assignment_delivery(db, assignment) == "core"


@pytest.mark.asyncio
async def test_a_rule_whose_outbound_changed_is_put_back_on_the_new_one(monkeypatch: pytest.MonkeyPatch):
    core = _Core(
        35, ["in-a"], outbounds=[{"tag": "zdrop", "protocol": "blackhole"}, {"tag": "DIRECT", "protocol": "freedom"}]
    )
    assignment = _Assignment(95, "in-a", node_id=350)
    db = _fleet(monkeypatch, [core], [_Node(350, 35)], [assignment])

    first = await service.push_live(db, 350)
    tag = first[0]["ruleTag"]
    assert {rule["outboundTag"] for rule in first} == {"zdrop"}

    core.config["outbounds"] = [{"tag": "adrop", "protocol": "blackhole"}, {"tag": "DIRECT", "protocol": "freedom"}]
    second = await service.push_live(db, 350)

    assert [rule["ruleTag"] for rule in second] == [tag]
    assert {rule["outboundTag"] for rule in second} == {"adrop"}
    assert [rule["outboundTag"] for rule in db.live[350].rules] == ["adrop"]
    assert db.live[350].removed == [tag]
    assert db.live[350].added == [tag, tag]


@pytest.mark.asyncio
async def test_a_rule_whose_outbound_is_unchanged_is_left_alone(monkeypatch: pytest.MonkeyPatch):
    core = _Core(36, ["in-a"])
    assignment = _Assignment(96, "in-a", node_id=360)
    db = _fleet(monkeypatch, [core], [_Node(360, 36)], [assignment])

    first = await service.push_live(db, 360)
    await service.push_live(db, 360)

    assert db.live[360].added == [first[0]["ruleTag"]]
    assert db.live[360].removed == []


@pytest.mark.asyncio
async def test_a_core_with_no_overlapping_rule_reports_no_advisory(monkeypatch: pytest.MonkeyPatch):
    core = _Core(37, ["in-a"])
    core.config["routing"] = {"rules": [{"ruleTag": "op-b", "outboundTag": "proxy", "inboundTag": ["in-b"]}]}
    assignment = _Assignment(97, "in-a", node_id=370)
    db = _fleet(monkeypatch, [core], [_Node(370, 37)], [assignment])

    _core_writer(monkeypatch, {})
    result = await service.apply_assignment(db, assignment, admin=object(), allow_restart=True)

    assert result["advisories"] == []
    assert result["advisory_note"] == ""
    assert assignment.enforced is True


@pytest.mark.asyncio
async def test_one_poisoned_profile_does_not_stop_the_other_filters_on_the_node(monkeypatch: pytest.MonkeyPatch):
    core = _Core(39, ["in-a", "in-b"])
    healthy = _Assignment(99, "in-a", node_id=390)
    poisoned = _Assignment(100, "in-b", node_id=390)
    poisoned.profile.block_list = ["domain:example.com"]
    db = _fleet(monkeypatch, [core], [_Node(390, 39)], [healthy, poisoned])

    pushed = await service.push_live(db, 390)

    assert _owners(pushed) == {99}
    assert _owners(db.live[390].rules) == {99}
    assert poisoned.enforced is False
    assert "domain:example.com" in poisoned.last_error
    assert healthy.enforced is True
    assert healthy.last_error is None


@pytest.mark.asyncio
async def test_a_poisoned_profile_does_not_stop_the_core_being_rebuilt(monkeypatch: pytest.MonkeyPatch):
    core = _Core(40, ["in-a", "in-b"])
    healthy = _Assignment(101, "in-a", node_id=400)
    poisoned = _Assignment(102, "in-b", node_id=400)
    poisoned.profile.block_list = ["regexp:.*ads.*"]
    db = _fleet(monkeypatch, [core], [_Node(400, 40)], [healthy, poisoned])

    assert _owners(await service.core_persisted_rules(db, 40)) == {101}

    captured = await _persist(monkeypatch, db, 40)
    assert _written_owners(captured["config"]) == {101}


@pytest.mark.asyncio
async def test_a_poisoned_profile_still_fails_its_own_apply(monkeypatch: pytest.MonkeyPatch):
    core = _Core(41, ["in-a"])
    poisoned = _Assignment(103, "in-a", node_id=410)
    poisoned.profile.block_list = ["geosite:ir"]
    db = _fleet(monkeypatch, [core], [_Node(410, 41)], [poisoned])

    with pytest.raises(service.EnforcementError) as refused:
        await service.apply_assignment(db, poisoned, admin=object(), allow_restart=True)

    assert refused.value.code == 422
    assert "geosite:ir" in refused.value.detail
    assert poisoned.enforced is False


@pytest.mark.asyncio
async def test_only_the_main_cores_rules_are_installed_live(monkeypatch: pytest.MonkeyPatch):
    primary = _Core(42, ["in-a"])
    attached = _Core(43, ["edge-in"])
    assignment = _Assignment(104, "", node_id=420)
    db = _fleet(monkeypatch, [primary, attached], [_Node(420, 42, extra_cores=[43])], [assignment])

    pushed = await service.push_live(db, 420)

    assert [rule["inboundTag"] for rule in pushed] == [["in-a"]]
    assert [rule["inboundTag"] for rule in db.live[420].rules] == [["in-a"]]
    assert await service.unreachable_cores(db, assignment, 420) == [43]
    assert [rule["inboundTag"] for rule in await service.core_persisted_rules(db, 43)] == [["edge-in"]]


@pytest.mark.asyncio
async def test_an_attached_core_rule_is_not_counted_as_delivered(monkeypatch: pytest.MonkeyPatch):
    primary = _Core(44, [("relay-in", "dokodemo-door")])
    attached = _Core(45, ["edge-in"])
    assignment = _Assignment(105, "", node_id=440)
    db = _fleet(monkeypatch, [primary, attached], [_Node(440, 44, extra_cores=[45])], [assignment])

    assert await service.push_live(db, 440) == []
    assert db.live[440].added == []

    _core_writer(monkeypatch, {})
    with pytest.raises(service.EnforcementError) as reported:
        await service.apply_assignment(db, assignment, admin=object(), allow_restart=True)

    assert "core 45" in reported.value.detail
    assert assignment.enforced is False


@pytest.mark.asyncio
async def test_an_allowed_domain_keeps_its_place_ahead_of_the_block_rules(monkeypatch: pytest.MonkeyPatch):
    core = _Core(
        46,
        ["in-a"],
        outbounds=[{"tag": "DIRECT", "protocol": "freedom"}, {"tag": "BLOCK", "protocol": "blackhole"}],
    )
    assignment = _Assignment(106, "in-a", node_id=460)
    assignment.profile.allow_list = ["allowed.example"]
    db = _fleet(monkeypatch, [core], [_Node(460, 46)], [assignment])

    first = await service.push_live(db, 460)
    order = [rule["ruleTag"] for rule in first]
    assert len(order) == 2
    assert first[0]["outboundTag"] == "DIRECT"
    assert first[1]["outboundTag"] == "BLOCK"
    assert db.live[460].tags() == order

    core.config["outbounds"] = [{"tag": "out", "protocol": "freedom"}, {"tag": "BLOCK", "protocol": "blackhole"}]
    second = await service.push_live(db, 460)

    assert [rule["ruleTag"] for rule in second] == order
    assert second[0]["outboundTag"] == "out"
    assert db.live[460].tags() == order
    assert [rule["outboundTag"] for rule in db.live[460].rules] == ["out", "BLOCK"]


@pytest.mark.asyncio
async def test_a_leftover_rule_puts_the_whole_set_back_in_order(monkeypatch: pytest.MonkeyPatch):
    core = _Core(47, ["in-a"])
    assignment = _Assignment(107, "in-a", node_id=470)
    assignment.profile.allow_list = ["allowed.example"]
    db = _fleet(monkeypatch, [core], [_Node(470, 47)], [assignment], live={470: ["pgcf-9-block"]})

    pushed = await service.push_live(db, 470)

    assert db.live[470].tags() == [rule["ruleTag"] for rule in pushed]
    assert "pgcf-9-block" in db.live[470].removed


@pytest.mark.asyncio
async def test_sniffing_that_only_records_metadata_is_not_enough(monkeypatch: pytest.MonkeyPatch):
    core = _Core(48, ["in-a"])
    metadata_only = {"enabled": True, "destOverride": ["http", "tls", "quic"], "metadataOnly": True}
    core.config["inbounds"][0]["sniffing"] = dict(metadata_only)
    assignment = _Assignment(108, "in-a", node_id=480)
    db = _fleet(monkeypatch, [core], [_Node(480, 48)], [assignment])

    assert service._sniffing_is_enough(metadata_only) is False

    captured: dict = {}
    _core_writer(monkeypatch, captured)
    with pytest.raises(service.ReloadRequired) as refused:
        await service.persist_core_rules(db, 48, admin=object())

    assert refused.value.inbound_tags == ["in-a"]
    assert "config" not in captured

    written = await _persist(monkeypatch, db, 48, allow_restart=True)
    assert _sniffing(written["config"], "in-a") == service.SNIFFING
    assert service._ledger_parts(db.overrides[0])[0] == metadata_only


@pytest.mark.asyncio
async def test_an_exclusion_list_is_reported_rather_than_assumed(monkeypatch: pytest.MonkeyPatch):
    core = _Core(49, ["in-a"])
    operator_choice = {
        "enabled": True,
        "destOverride": ["http", "tls", "quic"],
        "domainsExcluded": ["corp.example"],
    }
    core.config["inbounds"][0]["sniffing"] = dict(operator_choice)
    assignment = _Assignment(109, "in-a", node_id=490)
    db = _fleet(monkeypatch, [core], [_Node(490, 49)], [assignment])

    _core_writer(monkeypatch, {})
    result = await service.apply_assignment(db, assignment, admin=object(), allow_restart=True)

    assert any("corp.example" in notice for notice in result["advisories"])
    assert any("cannot be decided here" in notice for notice in result["advisories"])
    assert assignment.enforced is True
    assert db.added == []


@pytest.mark.asyncio
async def test_replacing_an_exclusion_list_says_so(monkeypatch: pytest.MonkeyPatch):
    core = _Core(50, ["in-a"])
    core.config["inbounds"][0]["sniffing"] = {"enabled": False, "domainsExcluded": ["corp.example"]}
    assignment = _Assignment(110, "in-a", node_id=500)
    db = _fleet(monkeypatch, [core], [_Node(500, 50)], [assignment])

    _core_writer(monkeypatch, {})
    result = await service.apply_assignment(db, assignment, admin=object(), allow_restart=True)

    assert any("replaced its exclusion list" in notice for notice in result["advisories"])
    assert any("corp.example" in notice for notice in result["advisories"])
    assert assignment.enforced is True


class _Session:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, *args):
        return False


@pytest.mark.asyncio
async def test_the_routing_service_check_reads_the_core_the_node_runs(monkeypatch: pytest.MonkeyPatch):
    import app.db as app_db

    main = _Core(51, ["in-a"])
    main.config["api"] = {"services": ["HandlerService", "StatsService"]}
    attached = _Core(52, ["edge-in"])
    attached.config["api"] = {"services": ["RoutingService"]}
    db = _fleet(monkeypatch, [main, attached], [_Node(510, 51, extra_cores=[52])], [])
    monkeypatch.setattr(app_db, "GetDB", lambda: _Session(db))

    assert await service.routing_service_declared(510) is False

    detail = await service._routing_service_detail(510)
    assert 'Add "RoutingService" to api.services in core 51' in detail
    assert "reconnect or restart the node" not in detail


@pytest.mark.asyncio
async def test_an_external_sniffing_edit_leaves_no_ledger_row_behind(monkeypatch: pytest.MonkeyPatch):
    core = _Core(53, ["in-a", "in-b"])
    assignment = _Assignment(111, "in-a", node_id=530)
    db = _fleet(monkeypatch, [core], [_Node(530, 53), _Node(531, 53)], [assignment])

    captured = await _persist(monkeypatch, db, 53)
    core.config = captured["config"]
    assert len(db.overrides) == 1

    core.config["inbounds"][0]["sniffing"] = {"enabled": True, "destOverride": ["fakedns"]}
    assignment.is_enabled = False
    again = await _persist(monkeypatch, db, 53)

    assert again["changed"] is False
    assert db.overrides == []
    assert _sniffing(core.config, "in-a") == {"enabled": True, "destOverride": ["fakedns"]}


@pytest.mark.asyncio
async def test_a_rebuild_that_drops_a_profile_says_that_filter_is_off(monkeypatch: pytest.MonkeyPatch):
    core = _Core(54, ["in-a", "in-b"])
    healthy = _Assignment(112, "in-a", node_id=540)
    poisoned = _Assignment(113, "in-b", node_id=540)
    poisoned.profile.block_list = ["geosite:ir"]
    db = _fleet(monkeypatch, [core], [_Node(540, 54)], [healthy, poisoned])

    captured = await _persist(monkeypatch, db, 54)

    assert _written_owners(captured["config"]) == {112}
    assert poisoned.enforced is False
    assert "geosite:ir" in poisoned.last_error
    assert healthy.enforced is True


@pytest.mark.asyncio
async def test_the_core_rebuild_itself_records_the_profile_it_dropped(monkeypatch: pytest.MonkeyPatch):
    core = _Core(56, ["in-a", "in-b"])
    healthy = _Assignment(116, "in-a", node_id=560)
    poisoned = _Assignment(117, "in-b", node_id=560)
    poisoned.profile.block_list = ["geosite:ir"]
    db = _fleet(monkeypatch, [core], [_Node(560, 56)], [healthy, poisoned])

    async def no_refresh(db_arg, core_id_arg):
        return []

    monkeypatch.setattr(service, "refresh_core_nodes", no_refresh)
    captured = await _persist(monkeypatch, db, 56)

    assert _written_owners(captured["config"]) == {116}
    assert poisoned.enforced is False
    assert "core 56" in poisoned.last_error
    assert "geosite:ir" in poisoned.last_error


@pytest.mark.asyncio
async def test_reading_a_cores_rules_never_writes_to_an_assignment(monkeypatch: pytest.MonkeyPatch):
    core = _Core(55, ["in-a", "in-b"])
    healthy = _Assignment(114, "in-a", node_id=550)
    poisoned = _Assignment(115, "in-b", node_id=550)
    poisoned.profile.block_list = ["geosite:ir"]
    db = _fleet(monkeypatch, [core], [_Node(550, 55)], [healthy, poisoned])
    commits = db.commits

    assert _owners(await service.core_persisted_rules(db, 55)) == {114}

    assert poisoned.enforced is True
    assert poisoned.last_error is None
    assert db.commits == commits


@pytest.mark.asyncio
async def test_switching_the_filter_off_asks_before_it_restarts_the_fleet(monkeypatch: pytest.MonkeyPatch):
    core = _Core(57, ["in-a"])
    core.config["inbounds"][0]["sniffing"] = {"enabled": False}
    assignment = _Assignment(118, "in-a", node_id=570)
    db = _fleet(monkeypatch, [core], [_Node(570, 57)], [assignment])

    installed = await _persist(monkeypatch, db, 57, allow_restart=True)
    core.config = installed["config"]
    assert _sniffing(core.config, "in-a") == service.SNIFFING

    assignment.is_enabled = False
    captured: dict = {}
    _core_writer(monkeypatch, captured)

    with pytest.raises(service.ReloadRequired) as refused:
        await service.persist_core_rules(db, 57, admin=object())

    assert refused.value.inbound_tags == ["in-a"]
    assert refused.value.node_ids == [570]
    assert "config" not in captured
    assert len(db.overrides) == 1


@pytest.mark.asyncio
async def test_an_authorised_withdrawal_puts_the_setting_back_and_reloads(monkeypatch: pytest.MonkeyPatch):
    core = _Core(58, ["in-a"])
    core.config["inbounds"][0]["sniffing"] = {"enabled": False}
    assignment = _Assignment(119, "in-a", node_id=580)
    db = _fleet(monkeypatch, [core], [_Node(580, 58)], [assignment])

    installed = await _persist(monkeypatch, db, 58, allow_restart=True)
    core.config = installed["config"]

    assignment.is_enabled = False
    restored = await _persist(monkeypatch, db, 58, allow_restart=True)

    assert _sniffing(restored["config"], "in-a") == {"enabled": False}
    assert restored["restarted"] == [58]
    assert db.overrides == []


@pytest.mark.asyncio
async def test_a_poisoned_profile_does_not_break_the_delivery_answer(monkeypatch: pytest.MonkeyPatch):
    core = _Core(59, ["in-a"])
    poisoned = _Assignment(120, "in-a", node_id=590)
    poisoned.profile.block_list = ["geosite:ir"]
    db = _fleet(monkeypatch, [core], [_Node(590, 59)], [poisoned])
    commits = db.commits

    assert await service.assignment_delivery(db, poisoned) == "live"
    assert poisoned.enforced is True
    assert poisoned.last_error is None
    assert db.commits == commits


@pytest.mark.asyncio
async def test_a_direct_outbound_that_redirects_is_not_used_for_the_allow_list(monkeypatch: pytest.MonkeyPatch):
    core = _Core(
        60,
        ["in-a"],
        outbounds=[
            {"tag": "DIRECT", "protocol": "freedom", "settings": {"redirect": "127.0.0.1:1080"}},
            {"tag": "plain", "protocol": "freedom"},
            {"tag": "BLOCK", "protocol": "blackhole"},
        ],
    )
    assignment = _Assignment(121, "in-a", node_id=600)
    assignment.profile.allow_list = ["allowed.example"]
    db = _fleet(monkeypatch, [core], [_Node(600, 60)], [assignment])

    pushed = await service.push_live(db, 600)

    assert [rule["outboundTag"] for rule in pushed] == ["plain", "BLOCK"]

    captured = await _persist(monkeypatch, db, 60)
    written = [rule for rule in captured["config"]["routing"]["rules"] if service.owns_tag(rule.get("ruleTag"))]
    assert [rule["outboundTag"] for rule in written] == ["plain", "BLOCK"]


@pytest.mark.asyncio
async def test_a_core_whose_only_direct_redirects_is_refused(monkeypatch: pytest.MonkeyPatch):
    core = _Core(
        61,
        ["in-a"],
        outbounds=[
            {"tag": "DIRECT", "protocol": "freedom", "settings": {"redirect": "10.0.0.9:443"}},
            {"tag": "BLOCK", "protocol": "blackhole"},
        ],
    )
    assignment = _Assignment(122, "in-a", node_id=610)
    assignment.profile.allow_list = ["allowed.example"]
    db = _fleet(monkeypatch, [core], [_Node(610, 61)], [assignment])

    with pytest.raises(service.EnforcementError) as refused:
        await service.push_live(db, 610)

    assert refused.value.code == 409
    assert "DIRECT" in refused.value.detail
    assert "keeps the destination" in refused.value.detail
    assert "BLOCK" in refused.value.detail
    assert db.live[610].added == []


@pytest.mark.asyncio
async def test_a_direct_outbound_that_only_sets_a_port_is_still_usable(monkeypatch: pytest.MonkeyPatch):
    core = _Core(
        62,
        ["in-a"],
        outbounds=[
            {"tag": "DIRECT", "protocol": "freedom", "settings": {"redirect": ":0"}},
            {"tag": "BLOCK", "protocol": "blackhole"},
        ],
    )
    assignment = _Assignment(123, "in-a", node_id=620)
    assignment.profile.allow_list = ["allowed.example"]
    db = _fleet(monkeypatch, [core], [_Node(620, 62)], [assignment])

    pushed = await service.push_live(db, 620)

    assert [rule["outboundTag"] for rule in pushed] == ["DIRECT", "BLOCK"]


def _strict(assignment: _Assignment) -> _Assignment:
    assignment.profile.strict_mode = True
    return assignment


@pytest.mark.asyncio
async def test_strict_mode_is_refused_where_its_catch_all_cannot_fire(monkeypatch: pytest.MonkeyPatch):
    core = _Core(63, ["in-a"])
    core.config["routing"] = {"domainStrategy": "AsIs"}
    assignment = _strict(_Assignment(124, "in-a", node_id=630))
    db = _fleet(monkeypatch, [core], [_Node(630, 63)], [assignment])

    with pytest.raises(service.EnforcementError) as refused:
        await service.push_live(db, 630)

    assert refused.value.code == 409
    assert "core 63" in refused.value.detail
    assert "IPIfNonMatch" in refused.value.detail
    assert "hostname traffic" in refused.value.detail
    assert "impossible" not in refused.value.detail
    assert db.live[630].added == []


@pytest.mark.asyncio
async def test_strict_mode_applies_where_the_core_resolves_before_matching(monkeypatch: pytest.MonkeyPatch):
    core = _Core(64, ["in-a"])
    core.config["routing"] = {"domainStrategy": "IPIfNonMatch"}
    assignment = _strict(_Assignment(125, "in-a", node_id=640))
    db = _fleet(monkeypatch, [core], [_Node(640, 64)], [assignment])

    pushed = await service.push_live(db, 640)

    assert any(service._is_strict(rule) for rule in pushed)
    assert _owners(db.live[640].rules) == {125}


@pytest.mark.asyncio
async def test_a_core_with_no_routing_section_cannot_take_strict_mode(monkeypatch: pytest.MonkeyPatch):
    core = _Core(65, ["in-a"])
    assignment = _strict(_Assignment(126, "in-a", node_id=650))
    db = _fleet(monkeypatch, [core], [_Node(650, 65)], [assignment])

    assert "routing" not in core.config
    assert service.strict_mode_effective(core.config) is False

    with pytest.raises(service.EnforcementError) as refused:
        await service.push_live(db, 650)

    assert refused.value.code == 409
    assert "core 65" in refused.value.detail


@pytest.mark.asyncio
async def test_a_profile_without_strict_mode_is_untouched_by_that_check(monkeypatch: pytest.MonkeyPatch):
    core = _Core(66, ["in-a"])
    assignment = _Assignment(127, "in-a", node_id=660)
    db = _fleet(monkeypatch, [core], [_Node(660, 66)], [assignment])

    pushed = await service.push_live(db, 660)

    assert _owners(pushed) == {127}
    assert not any(service._is_strict(rule) for rule in pushed)
    assert _owners(db.live[660].rules) == {127}
