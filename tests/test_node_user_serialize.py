from app.models.protocol import ProxyProtocol
from app.node.user import _serialize_user_for_node


def _openvpn_max_concurrent(hwid_limit):
    proto_user = _serialize_user_for_node(
        id=42,
        user_settings={"openvpn": {"password": "secret"}},
        inbounds=["openvpn-in"],
        allowed_protocols=frozenset({ProxyProtocol.openvpn}),
        hwid_limit=hwid_limit,
    )
    return proto_user.proxies.open_vpn.max_concurrent_connections


def test_openvpn_max_concurrent_connections_unset_hwid_limit_means_unlimited():
    assert _openvpn_max_concurrent(None) == 0


def test_openvpn_max_concurrent_connections_zero_hwid_limit_means_unlimited():
    assert _openvpn_max_concurrent(0) == 0


def test_openvpn_max_concurrent_connections_reuses_positive_hwid_limit():
    assert _openvpn_max_concurrent(5) == 5


def test_openvpn_username_and_password_still_set_regardless_of_hwid_limit():
    proto_user = _serialize_user_for_node(
        id=7,
        user_settings={"openvpn": {"password": "pw"}},
        inbounds=["openvpn-in"],
        allowed_protocols=frozenset({ProxyProtocol.openvpn}),
        hwid_limit=3,
    )
    assert proto_user.proxies.open_vpn.username == "7"
    assert proto_user.proxies.open_vpn.password == "pw"
    assert proto_user.proxies.open_vpn.max_concurrent_connections == 3
