import json

from app.models.subscription import SubscriptionInboundData, TCPTransportConfig, TLSConfig
from app.subscription.singbox import SingBoxConfiguration


def _inbound(protocol: str, finalmask: dict | None) -> SubscriptionInboundData:
    return SubscriptionInboundData(
        remark=protocol,
        inbound_tag=f"{protocol}-inbound",
        protocol=protocol,
        address="edge.example.com",
        port=443,
        network="tcp",
        tls_config=TLSConfig(tls=None),
        transport_config=TCPTransportConfig(),
        finalmask=finalmask,
        priority=0,
    )


def _render(protocol: str, finalmask: dict | None, settings: dict) -> dict:
    conf = SingBoxConfiguration()
    conf.add("r", "edge.example.com", _inbound(protocol, finalmask), settings)
    return json.loads(conf.render())["outbounds"][0]


def test_hysteria1_hop_ports_dash_range_normalized_to_colon():
    outbound = _render("hysteria", {"quicParams": {"udpHop": {"ports": "20000-29999"}}}, {"auth": "pw"})
    assert outbound["server_ports"] == ["20000:29999"]


def test_hysteria1_hop_ports_list_entries_normalized():
    outbound = _render(
        "hysteria", {"quicParams": {"udpHop": {"ports": ["20000-20010", "30000"]}}}, {"auth": "pw"}
    )
    assert outbound["server_ports"] == ["20000:20010", "30000"]


def test_hysteria2_hop_ports_still_normalized():
    outbound = _render("hysteria2", {"quicParams": {"udpHop": {"ports": "20000-29999"}}}, {"password": "pw"})
    assert outbound["server_ports"] == ["20000:29999"]


def test_hysteria1_without_hop_ports_has_no_server_ports():
    outbound = _render("hysteria", None, {"auth": "pw"})
    assert "server_ports" not in outbound
