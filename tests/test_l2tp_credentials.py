import pytest

from app.models.protocol import ProxyProtocol
from app.models.proxy import ProxyTable
from app.node.user import _serialize_user_for_node
from app.utils import l2tp as l2tp_utils


@pytest.mark.asyncio
async def test_prepare_l2tp_password_only_for_users_with_access(monkeypatch):
    async def has_access(db, groups):
        return False

    monkeypatch.setattr(l2tp_utils, "user_has_l2tp_access", has_access)
    settings = await l2tp_utils.prepare_l2tp_password(None, ProxyTable(), [])
    assert settings.l2tp.password is None


@pytest.mark.asyncio
async def test_prepare_l2tp_password_issues_once_and_never_rotates(monkeypatch):
    async def has_access(db, groups):
        return True

    monkeypatch.setattr(l2tp_utils, "user_has_l2tp_access", has_access)
    settings = await l2tp_utils.prepare_l2tp_password(None, ProxyTable(), [])
    issued = settings.l2tp.password
    assert issued and len(issued) == l2tp_utils.L2TP_PASSWORD_LENGTH

    again = await l2tp_utils.prepare_l2tp_password(None, settings, [])
    assert again.l2tp.password == issued


def test_stored_proxy_settings_without_l2tp_do_not_regenerate_on_read():
    stored = {
        "vmess": {"id": "b7b0f6ee-3e1b-4a9c-9d8e-4a0e1f2c3d4e"},
        "vless": {"id": "0f5f6b2c-1d3e-4a5b-8c7d-9e0f1a2b3c4d", "flow": None},
        "trojan": {"password": "legacy-trojan-password"},
        "shadowsocks": {"password": "legacy-shadowsocks-password", "method": "chacha20-ietf-poly1305"},
        "wireguard": {"private_key": None, "public_key": "legacy-wg-pub", "peer_ips": ["10.0.0.2/32"]},
        "hysteria": {"auth": "legacy-hysteria-auth"},
        "hysteria2": {"password": "legacy-hysteria2-password"},
        "openvpn": {"password": "legacy-openvpn-password"},
        "mtproto": {"secret": "ab" * 16},
        "tuic": {"uuid": "1c2d3e4f-5a6b-7c8d-9e0f-1a2b3c4d5e6f", "password": "legacy-tuic-password"},
    }
    assert "l2tp" not in stored

    first = ProxyTable.model_validate(stored)
    second = ProxyTable.model_validate(stored)
    assert first.l2tp.password is None
    assert second.l2tp.password is None
    assert first.trojan.password == "legacy-trojan-password"
    assert first.openvpn.password == "legacy-openvpn-password"
    assert first.mtproto.secret == "ab" * 16
    assert first.tuic.password == "legacy-tuic-password"
    assert stored == {**stored}

    proto = _serialize_user_for_node(9, stored, ["tag"], None, None)
    assert proto.proxies.l2tp.username == "9"
    assert proto.proxies.l2tp.password == ""
    assert proto.proxies.trojan.password == "legacy-trojan-password"
    assert proto.proxies.mtproto.username == "9"


def test_node_serializer_emits_l2tp_credentials_only_when_allowed():
    settings = {"l2tp": {"password": "Ab3xyzAb3xyzAb3xyzAb"}}
    allowed = _serialize_user_for_node(7, settings, ["l2tp-main"], frozenset({ProxyProtocol.l2tp}), None)
    assert allowed.proxies.l2tp.username == "7"
    assert allowed.proxies.l2tp.password == "Ab3xyzAb3xyzAb3xyzAb"
    assert list(allowed.inbounds) == ["l2tp-main"]

    denied = _serialize_user_for_node(7, settings, ["l2tp-main"], frozenset({ProxyProtocol.vless}), None)
    assert denied.proxies.l2tp.username == ""
    assert denied.proxies.l2tp.password == ""


def test_adding_l2tp_did_not_change_any_other_protocol_credential():
    from PasarGuardNodeBridge import create_proxy

    every_protocol = {
        "vmess": {"id": "b7b0f6ee-3e1b-4a9c-9d8e-4a0e1f2c3d4e"},
        "vless": {"id": "0f5f6b2c-1d3e-4a5b-8c7d-9e0f1a2b3c4d", "flow": "xtls-rprx-vision"},
        "trojan": {"password": "tj-pass"},
        "shadowsocks": {"password": "ss-pass", "method": "chacha20-ietf-poly1305"},
        "wireguard": {"public_key": "wg-pub", "peer_ips": ["10.0.0.2/32"]},
        "hysteria": {"auth": "hy-auth"},
        "hysteria2": {"password": "hy2-pass"},
        "openvpn": {"password": "ov-pass"},
        "mtproto": {"secret": "ab" * 16},
        "tuic": {"uuid": "1c2d3e4f-5a6b-7c8d-9e0f-1a2b3c4d5e6f", "password": "tuic-pass"},
        "l2tp": {"password": "l2tp-pass"},
    }
    proto = _serialize_user_for_node(42, every_protocol, ["tag"], None, 3)
    p = proto.proxies

    assert p.vmess.id == every_protocol["vmess"]["id"]
    assert (p.vless.id, p.vless.flow) == (every_protocol["vless"]["id"], "xtls-rprx-vision")
    assert p.trojan.password == "tj-pass"
    assert (p.shadowsocks.password, p.shadowsocks.method) == ("ss-pass", "chacha20-ietf-poly1305")
    assert (p.wireguard.public_key, list(p.wireguard.peer_ips)) == ("wg-pub", ["10.0.0.2/32"])
    assert p.hysteria.auth == "hy-auth"
    assert p.hysteria2.password == "hy2-pass"
    assert (p.open_vpn.username, p.open_vpn.password, p.open_vpn.max_concurrent_connections) == ("42", "ov-pass", 3)
    assert (p.mtproto.username, p.mtproto.secret) == ("42", "ab" * 16)
    assert (p.tuic.uuid, p.tuic.password) == (every_protocol["tuic"]["uuid"], "tuic-pass")
    assert (p.l2tp.username, p.l2tp.password) == ("42", "l2tp-pass")

    without_l2tp = create_proxy(
        mtproto_username="42", mtproto_secret="cd" * 16, openvpn_username="42", openvpn_password="x"
    )
    assert (without_l2tp.l2tp.username, without_l2tp.l2tp.password) == ("", "")
    assert without_l2tp.mtproto.username == "42"
    assert without_l2tp.open_vpn.password == "x"


@pytest.mark.parametrize(
    ("node_version", "refuses"),
    [
        ("", False),
        ("0.5.4", True),
        ("0.5.15", True),
        ("0.6.0", True),
        ("0.6.1", True),
        ("0.6.3", True),
        ("0.6.4", False),
        ("1.0.0", False),
        ("not-a-version", False),
        ("v0.5.4", True),
    ],
)
def test_only_a_known_pre_l2tp_node_version_is_refused(node_version, refuses):
    from app.operation.node import _node_lacks_l2tp

    assert _node_lacks_l2tp(node_version) is refuses


def test_the_refusal_message_names_the_version_and_the_fix():
    from app.operation.node import L2TP_MIN_NODE_VERSION, _l2tp_unsupported_message

    message = _l2tp_unsupported_message("0.5.4")
    assert "0.5.4" in message
    assert L2TP_MIN_NODE_VERSION in message
    assert "L2TP" in message
