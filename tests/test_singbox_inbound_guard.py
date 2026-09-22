import inspect
import json
import pathlib
import re
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.operation.core as core_operation_module
import app.operation.node as node_operation_module
from app.db.models import NodeStatus
from app.fork.cores import singbox_guard
from app.fork.cores.singbox import SingBoxConfig
from app.fork.operation import core_extras
from app.fork.operation.core_extras import guard_singbox_user_accounting
from app.models.core import CoreCreate, CoreType
from app.operation import OperatorType
from app.operation.core import CoreOperation
from app.operation.node import NodeOperation

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

HYSTERIA2_INBOUND = {
    "type": "hysteria2",
    "tag": "hy2-in",
    "listen_port": 2096,
    "tls": {"enabled": True, "server_name": "example.com"},
}

VLESS_INBOUND = {"type": "vless", "tag": "vless-in", "listen_port": 2095, "users": []}

VMESS_INBOUND = {"type": "vmess", "tag": "vmess-in", "listen_port": 2099, "users": []}

TROJAN_INBOUND = {"type": "trojan", "tag": "trojan-in", "listen_port": 2097, "users": []}

SHADOWSOCKS_INBOUND = {
    "type": "shadowsocks",
    "tag": "ss-in",
    "listen_port": 2100,
    "method": "aes-128-gcm",
    "users": [],
}

TUIC_INBOUND = {"type": "tuic", "tag": "tuic-in", "listen_port": 2098, "users": []}

MIXED_INBOUND = {"type": "mixed", "tag": "mixed-in", "listen_port": 1080}

FUTURE_INBOUND = {"type": "anytls", "tag": "anytls-in", "listen_port": 2101, "users": []}


def _core(inbounds, core_type=CoreType.singbox, name="benz"):
    return CoreCreate(
        name=name,
        type=core_type,
        config={"inbounds": list(inbounds), "outbounds": [{"type": "direct", "tag": "direct"}]},
        exclude_inbound_tags=set(),
        fallbacks_inbound_tags=set(),
    )


class _FakeCore(dict):
    def __init__(self, inbounds, core_type=CoreType.singbox):
        super().__init__({"inbounds": list(inbounds)})
        self.type = core_type


def _offenders(inbounds):
    return singbox_guard.singbox_index_keyed_inbounds(_FakeCore(inbounds))


async def test_a_hysteria2_only_singbox_core_is_accepted():
    assert await guard_singbox_user_accounting(None, _core([HYSTERIA2_INBOUND])) is None


async def test_a_non_singbox_core_carrying_a_vless_inbound_is_untouched():
    assert await guard_singbox_user_accounting(None, _core([VLESS_INBOUND], core_type=CoreType.xray)) is None


async def test_an_inbound_the_node_never_syncs_users_into_is_not_refused():
    assert await guard_singbox_user_accounting(None, _core([HYSTERIA2_INBOUND, MIXED_INBOUND])) is None


@pytest.mark.parametrize("inbound", [VLESS_INBOUND, VMESS_INBOUND, TROJAN_INBOUND, SHADOWSOCKS_INBOUND, TUIC_INBOUND])
async def test_every_index_keyed_protocol_is_refused_on_a_singbox_core(inbound):
    with pytest.raises(ValueError) as excinfo:
        await guard_singbox_user_accounting(None, _core([HYSTERIA2_INBOUND, inbound]))
    message = str(excinfo.value)
    assert inbound["tag"] in message
    assert inbound["type"] in message
    assert "deliberate safety gate" in message


async def test_an_inbound_type_this_panel_has_never_heard_of_is_refused_not_ignored():
    with pytest.raises(ValueError) as excinfo:
        await guard_singbox_user_accounting(None, _core([HYSTERIA2_INBOUND, FUTURE_INBOUND]))
    message = str(excinfo.value)
    assert "anytls-in (anytls)" in message
    assert "not known to this panel at all" in message


async def test_an_inbound_with_no_type_at_all_is_refused():
    with pytest.raises(ValueError) as excinfo:
        await guard_singbox_user_accounting(None, _core([{"tag": "nameless"}]))
    assert "nameless (untyped)" in str(excinfo.value)


async def test_the_refusal_names_every_offending_inbound_at_once():
    with pytest.raises(ValueError) as excinfo:
        await guard_singbox_user_accounting(None, _core([VLESS_INBOUND, TROJAN_INBOUND, HYSTERIA2_INBOUND]))
    message = str(excinfo.value)
    assert "vless-in (vless)" in message
    assert "trojan-in (trojan)" in message
    assert "hy2-in" not in message


async def test_the_refusal_explains_the_misattribution_and_the_panic():
    with pytest.raises(ValueError) as excinfo:
        await guard_singbox_user_accounting(None, _core([VLESS_INBOUND]))
    message = str(excinfo.value)
    assert "position in the inbound's user list" in message
    assert "different customer" in message
    assert "kills the node process" in message
    assert "Xray core" in message


def test_the_five_unported_protocols_have_no_known_fixed_build():
    assert set(singbox_guard.SINGBOX_NAME_KEYED_SINCE_NODE_VERSION) == {
        "vless",
        "vmess",
        "trojan",
        "shadowsocks",
        "tuic",
    }
    assert all(minimum is None for minimum in singbox_guard.SINGBOX_NAME_KEYED_SINCE_NODE_VERSION.values())
    assert singbox_guard.SINGBOX_NAME_KEYED_INBOUND_TYPES == frozenset({"hysteria2"})


def test_an_empty_users_list_does_not_make_an_inbound_look_safe():
    assert VLESS_INBOUND["users"] == []
    assert _offenders([VLESS_INBOUND]) == [("vless-in", "vless")]


def test_no_node_version_is_treated_as_name_keyed_while_no_fixed_build_is_known():
    for version in ("0.6.20", "999.0.0", "", None):
        assert singbox_guard.inbound_is_name_keyed_on("vless", version) is False
    assert singbox_guard.inbound_is_name_keyed_on("hysteria2", None) is True
    assert singbox_guard.inbound_is_name_keyed_on("mixed", None) is True


def test_the_node_attach_gate_refuses_an_index_keyed_inbound():
    message = singbox_guard.unsafe_singbox_node_message("Core 9", _offenders([VLESS_INBOUND]), "0.6.20")
    assert message is not None
    assert message.startswith("Core 9 declares")
    assert "vless-in (vless)" in message


def test_the_node_attach_gate_passes_a_hysteria2_only_core_without_needing_a_version():
    offenders = _offenders([HYSTERIA2_INBOUND])
    assert offenders == []
    assert singbox_guard.unsafe_singbox_node_message("Core 9", offenders, None) is None


def test_the_node_attach_gate_ignores_a_non_singbox_core():
    core = _FakeCore([VLESS_INBOUND], core_type=CoreType.xray)
    assert singbox_guard.singbox_index_keyed_inbounds(core) == []


def test_a_fixed_build_lifts_only_the_protocol_it_actually_fixed(monkeypatch):
    monkeypatch.setitem(singbox_guard.SINGBOX_NAME_KEYED_SINCE_NODE_VERSION, "vless", "0.7.0")

    offenders = _offenders([VLESS_INBOUND, TROJAN_INBOUND])

    blocked = singbox_guard.unsafe_singbox_node_message("Core 9", offenders, "0.7.1")
    assert blocked is not None
    assert "trojan-in (trojan)" in blocked
    assert "vless-in" not in blocked

    stale = singbox_guard.unsafe_singbox_node_message("Core 9", offenders, "0.6.20")
    assert "vless-in (vless)" in stale
    assert "trojan-in (trojan)" in stale


def test_a_fixed_build_lifts_the_gate_for_that_protocol_on_a_new_enough_node(monkeypatch):
    monkeypatch.setitem(singbox_guard.SINGBOX_NAME_KEYED_SINCE_NODE_VERSION, "vless", "0.7.0")

    offenders = _offenders([VLESS_INBOUND])
    assert singbox_guard.unsafe_singbox_node_message("Core 9", offenders, "0.7.0") is None
    assert singbox_guard.unsafe_singbox_node_message("Core 9", offenders, "0.7.1") is None
    assert singbox_guard.unsafe_singbox_node_message("Core 9", offenders, "0.6.20") is not None


def test_an_unparseable_or_missing_node_version_never_lifts_the_gate(monkeypatch):
    monkeypatch.setitem(singbox_guard.SINGBOX_NAME_KEYED_SINCE_NODE_VERSION, "vless", "0.7.0")

    for version in ("not-a-version", "", None, "None"):
        assert singbox_guard.inbound_is_name_keyed_on("vless", version) is False

    message = singbox_guard.unsafe_singbox_node_message("Core 9", _offenders([VLESS_INBOUND]), None)
    assert message is not None
    assert "reported no image version" in message


def test_a_config_with_no_inbounds_key_has_nothing_to_refuse():
    assert singbox_guard.index_keyed_inbounds({}) == []


def test_a_config_the_guard_cannot_read_is_refused_rather_than_waved_through():
    unreadable = singbox_guard.UNREADABLE_CONFIG
    assert singbox_guard.index_keyed_inbounds(None) == [unreadable]
    assert singbox_guard.index_keyed_inbounds("a json string") == [unreadable]
    assert singbox_guard.index_keyed_inbounds({"inbounds": "not-a-list"}) == [unreadable]
    assert singbox_guard.index_keyed_inbounds({"inbounds": [None, 7, {"type": "vless"}]}) == [
        unreadable,
        unreadable,
        ("vless", "vless"),
    ]
    assert singbox_guard.unsafe_singbox_node_message("Core 9", [unreadable], "999.0.0") is not None


async def test_a_serialized_config_string_is_refused_not_silently_allowed():
    core = _core([HYSTERIA2_INBOUND])
    core.config = '{"inbounds": []}'
    with pytest.raises(ValueError) as excinfo:
        await guard_singbox_user_accounting(None, core)
    assert "<whole config> (unreadable)" in str(excinfo.value)


def test_the_real_singbox_config_object_exposes_its_inbounds_to_the_attach_gate():
    core = SingBoxConfig(
        config={
            "inbounds": [HYSTERIA2_INBOUND, VLESS_INBOUND],
            "outbounds": [{"type": "direct", "tag": "direct"}],
        },
        exclude_inbound_tags=set(),
        fallbacks_inbound_tags=set(),
    )
    assert isinstance(core, dict)
    assert core.type == CoreType.singbox
    assert singbox_guard.singbox_index_keyed_inbounds(core) == [("vless-in", "vless")]


def test_a_rehydrated_singbox_config_still_exposes_its_inbounds_to_the_attach_gate():
    original = SingBoxConfig(
        config={
            "inbounds": [HYSTERIA2_INBOUND, TROJAN_INBOUND],
            "outbounds": [{"type": "direct", "tag": "direct"}],
        },
        exclude_inbound_tags=set(),
        fallbacks_inbound_tags=set(),
    )
    revived = SingBoxConfig.from_json(original.to_json())
    assert singbox_guard.singbox_index_keyed_inbounds(revived) == [("trojan-in", "trojan")]


def test_an_excluded_inbound_tag_does_not_hide_an_index_keyed_inbound_from_the_gate():
    core = SingBoxConfig(
        config={
            "inbounds": [HYSTERIA2_INBOUND, VLESS_INBOUND],
            "outbounds": [{"type": "direct", "tag": "direct"}],
        },
        exclude_inbound_tags={"vless-in"},
        fallbacks_inbound_tags=set(),
    )
    assert core.inbounds_by_tag.get("vless-in") is None
    assert singbox_guard.singbox_index_keyed_inbounds(core) == [("vless-in", "vless")]


GATED_DISPATCH_METHODS = {"start": "app/operation/node.py", "add_backend": "app/fork/operation/node_extras.py"}


def _rpc_methods_that_could_carry_config() -> set[str]:
    from PasarGuardNodeBridge import PasarGuardNode

    found = set()
    for name, fn in inspect.getmembers(PasarGuardNode, predicate=inspect.isfunction):
        if name.startswith("__"):
            continue
        parameters = inspect.signature(fn).parameters.values()
        if any(p.name == "config" or p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters):
            found.add(name)
    return found


def test_the_node_rpc_surface_carries_config_on_exactly_the_methods_this_guard_covers():
    assert _rpc_methods_that_could_carry_config() == set(GATED_DISPATCH_METHODS)


def test_every_place_the_panel_pushes_a_core_config_to_a_node_sits_behind_a_gate():
    panel = {
        path: (REPO_ROOT / path).read_text()
        for path in (
            "app/operation/node.py",
            "app/operation/core.py",
            "app/node/sync.py",
            "app/node/manager_sync.py",
            "app/fork/operation/node_extras.py",
        )
    }

    for method, owner in GATED_DISPATCH_METHODS.items():
        pattern = rf"pg_node\.{method}\("
        for path, source in panel.items():
            hits = len(re.findall(pattern, source))
            if path == owner:
                assert hits == 1, f"{path} should hold the only pg_node.{method} call, found {hits}"
            else:
                assert hits == 0, f"pg_node.{method} called outside {owner}, in {path}"

    assert panel["app/operation/node.py"].count("await NodeOperation._start_or_attach_node(") == 1


def test_serialising_a_core_for_dispatch_does_not_change_the_inbound_types_the_gate_saw():
    core = SingBoxConfig(
        config={
            "inbounds": [HYSTERIA2_INBOUND, dict(VLESS_INBOUND, up_mbps=100, down_mbps=100)],
            "outbounds": [{"type": "direct", "tag": "direct"}],
        },
        exclude_inbound_tags=set(),
        fallbacks_inbound_tags=set(),
    )
    dispatched = json.loads(core.to_str())
    assert singbox_guard.index_keyed_inbounds(dispatched) == singbox_guard.singbox_index_keyed_inbounds(core)
    assert [i["type"] for i in dispatched["inbounds"]] == [i["type"] for i in core["inbounds"]]


async def test_an_unsafe_edit_is_refused_while_a_node_on_that_core_still_runs_an_old_build(monkeypatch):
    monkeypatch.setitem(singbox_guard.SINGBOX_NAME_KEYED_SINCE_NODE_VERSION, "vless", "0.7.0")

    async def _versions(db, core_id):
        return [("frankfurt", "0.7.1"), ("tehran", "0.6.20"), ("baku", None)]

    monkeypatch.setattr(core_extras, "get_node_versions_by_core", _versions)

    with pytest.raises(ValueError) as excinfo:
        await guard_singbox_user_accounting(None, _core([VLESS_INBOUND]), 9)
    message = str(excinfo.value)
    assert "tehran (v0.6.20)" in message
    assert "baku (no reported version)" in message
    assert "frankfurt" not in message
    assert "vless-in (vless)" in message


async def test_the_same_edit_is_accepted_once_every_node_on_that_core_runs_a_fixed_build(monkeypatch):
    monkeypatch.setitem(singbox_guard.SINGBOX_NAME_KEYED_SINCE_NODE_VERSION, "vless", "0.7.0")

    async def _versions(db, core_id):
        return [("frankfurt", "0.7.1"), ("tehran", "0.7.0")]

    monkeypatch.setattr(core_extras, "get_node_versions_by_core", _versions)

    assert await guard_singbox_user_accounting(None, _core([VLESS_INBOUND]), 9) is None


async def test_a_protocol_with_no_fixed_build_is_refused_before_any_node_is_consulted(monkeypatch):
    async def _boom(db, core_id):
        raise AssertionError("an unconditionally unsafe protocol must not need a node lookup")

    monkeypatch.setattr(core_extras, "get_node_versions_by_core", _boom)

    with pytest.raises(ValueError) as excinfo:
        await guard_singbox_user_accounting(None, _core([TROJAN_INBOUND]), 9)
    assert "no node build is known" in str(excinfo.value)


async def test_a_brand_new_core_has_no_nodes_to_check_and_is_left_to_the_attach_gate(monkeypatch):
    monkeypatch.setitem(singbox_guard.SINGBOX_NAME_KEYED_SINCE_NODE_VERSION, "vless", "0.7.0")

    async def _boom(db, core_id):
        raise AssertionError("a core with no id must not be queried for nodes")

    monkeypatch.setattr(core_extras, "get_node_versions_by_core", _boom)

    assert await guard_singbox_user_accounting(None, _core([VLESS_INBOUND])) is None


PAST_THE_CORE_GATE = "core validation was reached"

ADMIN = SimpleNamespace(username="tester", is_sudo=True)


class _FakeSession:
    def __init__(self):
        self.rollbacks = 0

    async def rollback(self):
        self.rollbacks += 1


class _FakeNode:
    def __init__(self, version=""):
        self.version = version
        self.started = []
        self.added = []
        self.stopped = 0

    async def get_health(self):
        return None

    async def get_versions(self):
        return self.version, "1.2.3"

    async def get_lifecycle_state(self):
        return None

    async def node_version(self):
        return self.version

    async def info(self):
        return None

    async def stop(self):
        self.stopped += 1

    async def start(self, **kwargs):
        self.started.append(kwargs)
        return SimpleNamespace(node_version=self.version or "unreported", core_version="1.2.3")

    async def list_backends(self):
        return None

    async def add_backend(self, **kwargs):
        self.added.append(kwargs)

    async def remove_backend(self, **kwargs):
        return None


def _singbox_core(inbounds, exclude=()):
    return SingBoxConfig(
        config={"inbounds": list(inbounds), "outbounds": [{"type": "direct", "tag": "direct"}]},
        exclude_inbound_tags=set(exclude),
        fallbacks_inbound_tags=set(),
    )


def _db_node(node_id):
    return SimpleNamespace(id=node_id, name="tehran", status=NodeStatus.connecting, keep_alive=0)


def _core_operation(monkeypatch):
    def _reached(*args, **kwargs):
        raise RuntimeError(PAST_THE_CORE_GATE)

    monkeypatch.setattr(core_operation_module.core_manager, "validate_core", _reached)
    return CoreOperation(OperatorType.API)


def _attach_node(monkeypatch, pg_node):
    async def _get_node(node_id):
        return pg_node

    monkeypatch.setattr(node_operation_module.node_manager, "get_node", _get_node)
    return pg_node


async def _connect(node_id, core):
    return await NodeOperation.connect_node(_db_node(node_id), core, [], None, force_start=True)


async def test_create_core_refuses_an_index_keyed_singbox_core_instead_of_storing_it(monkeypatch):
    operation = _core_operation(monkeypatch)
    db = _FakeSession()

    with pytest.raises(HTTPException) as excinfo:
        await operation.create_core(db, _core([HYSTERIA2_INBOUND, VLESS_INBOUND]), ADMIN)

    assert excinfo.value.status_code == 400
    assert "vless-in (vless)" in excinfo.value.detail
    assert "deliberate safety gate" in excinfo.value.detail
    assert PAST_THE_CORE_GATE not in excinfo.value.detail
    assert db.rollbacks == 1


async def test_create_core_lets_a_hysteria2_only_singbox_core_through_to_validation(monkeypatch):
    operation = _core_operation(monkeypatch)

    with pytest.raises(HTTPException) as excinfo:
        await operation.create_core(_FakeSession(), _core([HYSTERIA2_INBOUND]), ADMIN)

    assert PAST_THE_CORE_GATE in excinfo.value.detail


async def test_create_core_refuses_a_singbox_core_whose_config_cannot_be_read(monkeypatch):
    operation = _core_operation(monkeypatch)
    unreadable = _core([HYSTERIA2_INBOUND])
    unreadable.config = '{"inbounds": []}'

    with pytest.raises(HTTPException) as excinfo:
        await operation.create_core(_FakeSession(), unreadable, ADMIN)

    assert "<whole config> (unreadable)" in excinfo.value.detail
    assert PAST_THE_CORE_GATE not in excinfo.value.detail


async def test_modify_core_refuses_an_edit_a_live_node_on_that_core_cannot_account_for(monkeypatch):
    monkeypatch.setitem(singbox_guard.SINGBOX_NAME_KEYED_SINCE_NODE_VERSION, "vless", "0.7.0")
    operation = _core_operation(monkeypatch)

    async def _stored(db, core_id):
        return SimpleNamespace(id=core_id, type=CoreType.singbox, name="benz")

    async def _versions(db, core_id):
        assert core_id == 9
        return [("frankfurt", "0.7.1"), ("tehran", "0.6.20")]

    monkeypatch.setattr(operation, "get_validated_core_config", _stored)
    monkeypatch.setattr(core_extras, "get_node_versions_by_core", _versions)

    with pytest.raises(HTTPException) as excinfo:
        await operation.modify_core(_FakeSession(), 9, _core([VLESS_INBOUND]), ADMIN)

    assert "tehran (v0.6.20)" in excinfo.value.detail
    assert "frankfurt" not in excinfo.value.detail
    assert PAST_THE_CORE_GATE not in excinfo.value.detail


async def test_modify_core_accepts_the_same_edit_once_every_node_runs_a_fixed_build(monkeypatch):
    monkeypatch.setitem(singbox_guard.SINGBOX_NAME_KEYED_SINCE_NODE_VERSION, "vless", "0.7.0")
    operation = _core_operation(monkeypatch)

    async def _stored(db, core_id):
        return SimpleNamespace(id=core_id, type=CoreType.singbox, name="benz")

    async def _versions(db, core_id):
        return [("frankfurt", "0.7.1"), ("tehran", "0.7.0")]

    monkeypatch.setattr(operation, "get_validated_core_config", _stored)
    monkeypatch.setattr(core_extras, "get_node_versions_by_core", _versions)

    with pytest.raises(HTTPException) as excinfo:
        await operation.modify_core(_FakeSession(), 9, _core([VLESS_INBOUND]), ADMIN)

    assert PAST_THE_CORE_GATE in excinfo.value.detail


async def test_modify_core_refuses_a_protocol_no_node_build_can_account_for(monkeypatch):
    operation = _core_operation(monkeypatch)

    async def _stored(db, core_id):
        return SimpleNamespace(id=core_id, type=CoreType.singbox, name="benz")

    async def _boom(db, core_id):
        raise AssertionError("an unconditionally unsafe protocol must not need a node lookup")

    monkeypatch.setattr(operation, "get_validated_core_config", _stored)
    monkeypatch.setattr(core_extras, "get_node_versions_by_core", _boom)

    with pytest.raises(HTTPException) as excinfo:
        await operation.modify_core(_FakeSession(), 9, _core([TROJAN_INBOUND]), ADMIN)

    assert "trojan-in (trojan)" in excinfo.value.detail
    assert "no node build is known" in excinfo.value.detail
    assert PAST_THE_CORE_GATE not in excinfo.value.detail


async def test_connecting_a_node_never_starts_an_index_keyed_singbox_core(monkeypatch):
    pg_node = _attach_node(monkeypatch, _FakeNode("0.6.20"))

    result = await _connect(9001, _singbox_core([HYSTERIA2_INBOUND, VLESS_INBOUND]))

    assert result["status"] is NodeStatus.error
    assert "vless-in (vless)" in result["message"]
    assert "deliberate safety gate" in result["message"]
    assert result["node_version"] == "0.6.20"
    assert pg_node.started == []


async def test_connecting_a_node_starts_a_hysteria2_only_singbox_core(monkeypatch):
    pg_node = _attach_node(monkeypatch, _FakeNode("0.6.20"))

    result = await _connect(9002, _singbox_core([HYSTERIA2_INBOUND]))

    assert result["status"] is NodeStatus.connected
    assert len(pg_node.started) == 1
    assert json.loads(pg_node.started[0]["config"])["inbounds"][0]["type"] == "hysteria2"


async def test_connecting_a_node_never_starts_a_singbox_core_it_cannot_read(monkeypatch):
    pg_node = _attach_node(monkeypatch, _FakeNode("999.0.0"))
    broken = SimpleNamespace(type=CoreType.singbox, to_str=lambda: "not json at all")

    result = await _connect(9003, broken)

    assert result["status"] is NodeStatus.error
    assert "<whole config> (unreadable)" in result["message"]
    assert pg_node.started == []


async def test_connecting_a_node_that_reports_no_version_refuses_an_index_keyed_core(monkeypatch):
    monkeypatch.setitem(singbox_guard.SINGBOX_NAME_KEYED_SINCE_NODE_VERSION, "vless", "0.7.0")
    pg_node = _attach_node(monkeypatch, _FakeNode(""))

    result = await _connect(9004, _singbox_core([VLESS_INBOUND]))

    assert result["status"] is NodeStatus.error
    assert "reported no image version" in result["message"]
    assert pg_node.started == []


async def test_connecting_a_node_on_a_fixed_build_starts_the_same_core(monkeypatch):
    monkeypatch.setitem(singbox_guard.SINGBOX_NAME_KEYED_SINCE_NODE_VERSION, "vless", "0.7.0")
    pg_node = _attach_node(monkeypatch, _FakeNode("0.7.1"))

    result = await _connect(9005, _singbox_core([VLESS_INBOUND]))

    assert result["status"] is NodeStatus.connected
    assert len(pg_node.started) == 1


async def test_connecting_a_node_refuses_an_index_keyed_inbound_hidden_by_an_excluded_tag(monkeypatch):
    pg_node = _attach_node(monkeypatch, _FakeNode("0.6.20"))

    result = await _connect(9006, _singbox_core([HYSTERIA2_INBOUND, VLESS_INBOUND], exclude=["vless-in"]))

    assert result["status"] is NodeStatus.error
    assert "vless-in (vless)" in result["message"]
    assert pg_node.started == []


async def test_an_extra_singbox_core_with_an_index_keyed_inbound_is_never_added_to_a_node():
    pg_node = _FakeNode("0.6.20")

    problems = await NodeOperation._add_extra_cores(
        pg_node, _db_node(9101), [(7, _singbox_core([HYSTERIA2_INBOUND, TROJAN_INBOUND]), [])]
    )

    assert "core 7:" in problems
    assert "trojan-in (trojan)" in problems
    assert "deliberate safety gate" in problems
    assert pg_node.added == []


async def test_an_extra_hysteria2_only_singbox_core_is_added_to_a_node():
    pg_node = _FakeNode("0.6.20")

    problems = await NodeOperation._add_extra_cores(
        pg_node, _db_node(9102), [(7, _singbox_core([HYSTERIA2_INBOUND]), [])]
    )

    assert problems == ""
    assert len(pg_node.added) == 1
    assert json.loads(pg_node.added[0]["config"])["inbounds"][0]["type"] == "hysteria2"


async def test_an_extra_singbox_core_the_panel_cannot_read_is_never_added_to_a_node():
    pg_node = _FakeNode("999.0.0")
    broken = SimpleNamespace(type=CoreType.singbox, to_str=lambda: "not json at all")

    problems = await NodeOperation._add_extra_cores(pg_node, _db_node(9103), [(7, broken, [])])

    assert "<whole config> (unreadable)" in problems
    assert pg_node.added == []


async def test_an_extra_singbox_core_is_refused_on_a_node_that_reports_no_version(monkeypatch):
    monkeypatch.setitem(singbox_guard.SINGBOX_NAME_KEYED_SINCE_NODE_VERSION, "vless", "0.7.0")
    pg_node = _FakeNode("")

    problems = await NodeOperation._add_extra_cores(pg_node, _db_node(9104), [(7, _singbox_core([VLESS_INBOUND]), [])])

    assert "reported no image version" in problems
    assert pg_node.added == []


async def test_an_extra_singbox_core_is_added_once_the_node_runs_a_fixed_build(monkeypatch):
    monkeypatch.setitem(singbox_guard.SINGBOX_NAME_KEYED_SINCE_NODE_VERSION, "vless", "0.7.0")
    pg_node = _FakeNode("0.7.1")

    problems = await NodeOperation._add_extra_cores(pg_node, _db_node(9105), [(7, _singbox_core([VLESS_INBOUND]), [])])

    assert problems == ""
    assert len(pg_node.added) == 1


def test_the_core_ingestion_guard_is_a_coroutine_so_it_cannot_be_awaited_by_accident():
    assert inspect.iscoroutinefunction(guard_singbox_user_accounting)


XRAY_INBOUND = {"protocol": "vless", "tag": "xray-vless-in", "port": 443, "settings": {"clients": []}}


async def test_a_real_xray_shaped_core_is_never_touched_by_the_singbox_guard():
    core = _core([XRAY_INBOUND, {"protocol": "vmess", "tag": "xray-vmess-in", "port": 80}], core_type=CoreType.xray)
    assert await guard_singbox_user_accounting(None, core) is None
    assert singbox_guard.unsafe_singbox_core_message(CoreType.xray, "x", core.config) is None
    assert singbox_guard.unsafe_singbox_core_nodes_message(CoreType.xray, "x", core.config, [("n", None)]) is None


def test_an_xray_core_object_is_never_inspected_by_the_node_attach_gate():
    core = _FakeCore([XRAY_INBOUND], core_type=CoreType.xray)
    assert singbox_guard.singbox_index_keyed_inbounds(core) == []
    assert singbox_guard.unsafe_singbox_node_message("Core 1", [], None) is None


def test_the_attach_gate_reads_the_exact_bytes_that_will_be_dispatched():
    core = SingBoxConfig(
        config={
            "inbounds": [HYSTERIA2_INBOUND, VLESS_INBOUND],
            "outbounds": [{"type": "direct", "tag": "direct"}],
        },
        exclude_inbound_tags=set(),
        fallbacks_inbound_tags=set(),
    )
    assert singbox_guard.dispatched_config(core) == json.loads(core.to_str())
    assert singbox_guard.singbox_index_keyed_inbounds(core) == [("vless-in", "vless")]


def test_a_core_whose_wire_config_cannot_be_parsed_is_refused():
    broken = SimpleNamespace(type=CoreType.singbox, to_str=lambda: "not json at all")
    assert singbox_guard.singbox_index_keyed_inbounds(broken) == [singbox_guard.UNREADABLE_CONFIG]

    def _explode():
        raise RuntimeError("serialisation failed")

    exploding = SimpleNamespace(type=CoreType.singbox, to_str=_explode)
    assert singbox_guard.singbox_index_keyed_inbounds(exploding) == [singbox_guard.UNREADABLE_CONFIG]

    message = singbox_guard.unsafe_singbox_node_message("Core 9", [singbox_guard.UNREADABLE_CONFIG], "999.0.0")
    assert message is not None
    assert "<whole config> (unreadable)" in message


def test_a_core_serialising_to_an_empty_wire_config_has_nothing_to_refuse():
    empty = SimpleNamespace(type=CoreType.singbox, to_str=lambda: "{}")
    assert singbox_guard.singbox_index_keyed_inbounds(empty) == []


def test_serialising_a_core_twice_yields_the_same_bytes_so_the_gate_judges_what_is_dispatched():
    core = SingBoxConfig(
        config={
            "inbounds": [HYSTERIA2_INBOUND, dict(VLESS_INBOUND, up_mbps=100)],
            "outbounds": [{"type": "direct", "tag": "direct"}],
        },
        exclude_inbound_tags=set(),
        fallbacks_inbound_tags=set(),
    )
    first = core.to_str()
    assert core.to_str() == first
    assert core.to_str() == first
    assert singbox_guard.dispatched_config(core) == json.loads(first)


def test_serialising_a_core_does_not_mutate_it():
    inbounds = [HYSTERIA2_INBOUND, dict(VLESS_INBOUND, up_mbps=100, down_mbps=50)]
    core = SingBoxConfig(
        config={"inbounds": inbounds, "outbounds": [{"type": "direct", "tag": "direct"}]},
        exclude_inbound_tags=set(),
        fallbacks_inbound_tags=set(),
    )
    before = deepcopy(dict(core))
    core.to_str()
    assert dict(core) == before
