import json

from app.models.subscription import SubscriptionInboundData, TCPTransportConfig, TLSConfig
from app.subscription.singbox import SingBoxConfiguration


def _inbound(remark: str) -> SubscriptionInboundData:
    return SubscriptionInboundData(
        remark=remark,
        inbound_tag=f"{remark}-inbound",
        protocol="wireguard",
        address="edge.example.com",
        port=51820,
        network="udp",
        tls_config=TLSConfig(tls=None),
        transport_config=TCPTransportConfig(),
        wireguard_public_key="server-pub",
        priority=0,
    )


def _settings() -> dict:
    return {"private_key": "client-priv", "peer_ips": ["10.0.0.2/32"]}


def test_multiple_wireguard_hosts_get_unique_interface_names():
    conf = SingBoxConfiguration()
    conf.add("wg-a", "a.example.com", _inbound("wg-a"), _settings())
    conf.add("wg-b", "b.example.com", _inbound("wg-b"), _settings())
    conf.add("wg-c", "c.example.com", _inbound("wg-c"), _settings())

    endpoints = json.loads(conf.render())["endpoints"]
    names = [endpoint["name"] for endpoint in endpoints]
    assert names == ["wg0", "wg1", "wg2"]
    assert len(set(names)) == 3


def test_single_wireguard_host_keeps_wg0():
    conf = SingBoxConfiguration()
    conf.add("wg-a", "a.example.com", _inbound("wg-a"), _settings())

    endpoints = json.loads(conf.render())["endpoints"]
    assert endpoints[0]["name"] == "wg0"
