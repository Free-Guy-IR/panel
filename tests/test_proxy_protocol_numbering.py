from app.models.protocol import ProxyProtocol

RESERVED_VALUES = {9}
RESERVED_NAMES = {"mtproto"}

PINNED = {
    "vmess": 1,
    "vless": 2,
    "trojan": 3,
    "shadowsocks": 4,
    "wireguard": 5,
    "hysteria": 6,
    "hysteria2": 7,
    "openvpn": 8,
    "tuic": 10,
    "l2tp": 11,
    "masque": 12,
}


def test_every_protocol_keeps_its_number():
    assert {protocol.name: protocol.value for protocol in ProxyProtocol} == PINNED


def test_reserved_values_are_not_reused():
    taken = {protocol.value for protocol in ProxyProtocol}
    assert taken & RESERVED_VALUES == set()


def test_reserved_names_are_not_reused():
    assert {protocol.name for protocol in ProxyProtocol} & RESERVED_NAMES == set()


def test_from_value_refuses_a_reserved_name():
    for name in RESERVED_NAMES:
        assert ProxyProtocol.from_value(name) is None


def test_masque_is_its_own_member_and_not_a_hysteria2_alias():
    assert ProxyProtocol.from_value("masque") is ProxyProtocol.masque
    assert ProxyProtocol.masque is not ProxyProtocol.hysteria2
    assert ProxyProtocol.masque not in frozenset({ProxyProtocol.hysteria2})
