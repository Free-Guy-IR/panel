import json

from app.models.subscription import SubscriptionInboundData, TCPTransportConfig, TLSConfig
from app.subscription import OutlineConfiguration


def _inbound(remark: str, port: int) -> SubscriptionInboundData:
    return SubscriptionInboundData(
        remark=remark,
        inbound_tag=f"{remark}-inbound",
        protocol="shadowsocks",
        address=f"{remark}.example.com",
        port=port,
        network="tcp",
        tls_config=TLSConfig(tls=None),
        transport_config=TCPTransportConfig(),
        priority=0,
    )


def _settings() -> dict:
    return {"method": "chacha20-ietf-poly1305", "password": "secret"}


def test_outline_keeps_first_shadowsocks_host():
    conf = OutlineConfiguration()
    conf.add("first", "first.example.com", _inbound("first", 8388), _settings())
    conf.add("second", "second.example.com", _inbound("second", 8389), _settings())

    config = json.loads(conf.render())
    assert config["server"] == "first.example.com"
    assert config["server_port"] == 8388


def test_outline_ignores_non_shadowsocks_protocols():
    conf = OutlineConfiguration()
    vless_inbound = _inbound("vless", 443).model_copy(update={"protocol": "vless"})
    conf.add("vless", "vless.example.com", vless_inbound, {"id": "11111111-1111-1111-1111-111111111111"})

    assert json.loads(conf.render()) == {}
