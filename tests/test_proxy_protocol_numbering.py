from app.models.protocol import ProxyProtocol
from app.node.user import _serialize_user_for_node

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


def _top_level_field_numbers(raw: bytes) -> set[int]:
    numbers: set[int] = set()
    index = 0
    while index < len(raw):
        key = 0
        shift = 0
        while True:
            byte = raw[index]
            index += 1
            key |= (byte & 0x7F) << shift
            if not byte & 0x80:
                break
            shift += 7
        numbers.add(key >> 3)
        wire = key & 7
        if wire == 0:
            while raw[index] & 0x80:
                index += 1
            index += 1
        elif wire == 1:
            index += 8
        elif wire == 2:
            length = 0
            shift = 0
            while True:
                byte = raw[index]
                index += 1
                length |= (byte & 0x7F) << shift
                if not byte & 0x80:
                    break
                shift += 7
            index += length
        elif wire == 5:
            index += 4
        else:
            raise ValueError(f"unsupported wire type {wire} at offset {index}")
    return numbers


def test_the_wire_decoder_sees_the_fields_that_are_there():
    from PasarGuardNodeBridge import create_proxy

    raw = create_proxy(vless_id="11111111-2222-3333-4444-555555555555").SerializeToString()
    assert 2 in _top_level_field_numbers(raw)


def test_a_revoked_user_sends_an_empty_proxy():
    from PasarGuardNodeBridge import create_proxy

    from app.fork.node_credentials import omit_empty_credentials

    raw = omit_empty_credentials(create_proxy()).SerializeToString()
    assert _top_level_field_numbers(raw) == set()
    assert raw == b""


def test_no_reserved_number_reaches_the_wire_for_a_real_user():
    proto_user = _serialize_user_for_node(
        id=52,
        user_settings={"vless": {"id": "11111111-2222-3333-4444-555555555555"}},
        inbounds=["vless-in"],
        allowed_protocols=frozenset({ProxyProtocol.vless}),
        hwid_limit=None,
    )
    raw = proto_user.proxies.SerializeToString()
    assert _top_level_field_numbers(raw) & RESERVED_VALUES == set()


def test_a_reserved_name_is_never_set_on_the_proxy_message():
    from PasarGuardNodeBridge import create_proxy

    from app.fork.node_credentials import omit_empty_credentials

    proxy = omit_empty_credentials(create_proxy(vless_id="11111111-2222-3333-4444-555555555555"))
    declared = {field.name for field in proxy.DESCRIPTOR.fields}
    for name in RESERVED_NAMES & declared:
        assert not proxy.HasField(name)
