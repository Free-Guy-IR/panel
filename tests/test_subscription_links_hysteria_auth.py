from app.models.subscription import SubscriptionInboundData, TCPTransportConfig, TLSConfig
from app.subscription.links import StandardLinks


def _inbound(protocol: str) -> SubscriptionInboundData:
    return SubscriptionInboundData(
        remark=protocol,
        inbound_tag=f"{protocol}-inbound",
        protocol=protocol,
        address="edge.example.com",
        port=443,
        network="tcp",
        tls_config=TLSConfig(tls=None),
        transport_config=TCPTransportConfig(),
        priority=0,
    )


def test_hysteria1_auth_is_url_encoded():
    links = StandardLinks()
    links.add("r", "edge.example.com", _inbound("hysteria"), {"auth": "p@ss:w/rd?#"})
    link = links.links[0]
    assert link.startswith("hysteria2://p%40ss%3Aw%2Frd%3F%23@edge.example.com:443")


def test_hysteria2_password_is_url_encoded():
    links = StandardLinks()
    links.add("r", "edge.example.com", _inbound("hysteria2"), {"password": "p@ss:w/rd?#"})
    link = links.links[0]
    assert link.startswith("hysteria2://p%40ss%3Aw%2Frd%3F%23@edge.example.com:443")
