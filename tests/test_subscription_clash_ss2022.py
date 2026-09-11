import base64

from app.models.subscription import SubscriptionInboundData, TCPTransportConfig, TLSConfig
from app.subscription.clash import ClashConfiguration, ClashMetaConfiguration

INBOUND_KEY = base64.b64encode(b"0" * 16).decode()


def _inbound(is_2022: bool, method: str = "") -> SubscriptionInboundData:
    return SubscriptionInboundData(
        remark="ss",
        inbound_tag="ss-inbound",
        protocol="shadowsocks",
        address="edge.example.com",
        port=8388,
        network="tcp",
        tls_config=TLSConfig(tls=None),
        transport_config=TCPTransportConfig(),
        is_2022=is_2022,
        method=method,
        password=INBOUND_KEY if is_2022 else "",
        priority=0,
    )


def test_clash_classic_skips_ss2022_inbound():
    clash = ClashConfiguration()
    clash.add(
        "ss 2022",
        "edge.example.com",
        _inbound(is_2022=True, method="2022-blake3-aes-128-gcm"),
        {"method": "aes-128-gcm", "password": "user-password"},
    )

    assert clash.data["proxies"] == []


def test_clash_classic_still_emits_plain_shadowsocks():
    clash = ClashConfiguration()
    clash.add(
        "ss plain",
        "edge.example.com",
        _inbound(is_2022=False),
        {"method": "aes-256-gcm", "password": "user-password"},
    )

    assert len(clash.data["proxies"]) == 1
    node = clash.data["proxies"][0]
    assert node["cipher"] == "aes-256-gcm"
    assert node["password"] == "user-password"


def test_clash_meta_emits_ss2022_with_combined_password():
    meta = ClashMetaConfiguration()
    meta.add(
        "ss 2022",
        "edge.example.com",
        _inbound(is_2022=True, method="2022-blake3-aes-128-gcm"),
        {"method": "aes-128-gcm", "password": "user-password"},
    )

    assert len(meta.data["proxies"]) == 1
    node = meta.data["proxies"][0]
    assert node["method"] == "2022-blake3-aes-128-gcm"
    inbound_part, user_part = node["password"].split(":", 1)
    assert inbound_part == INBOUND_KEY
    assert len(base64.b64decode(user_part)) == 16
