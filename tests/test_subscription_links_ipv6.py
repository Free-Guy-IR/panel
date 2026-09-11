from app.models.subscription import SubscriptionInboundData, TCPTransportConfig, TLSConfig
from app.subscription.links import StandardLinks

USER_ID = "11111111-1111-1111-1111-111111111111"
IPV6 = "2001:db8::1"


def _inbound(protocol: str) -> SubscriptionInboundData:
    return SubscriptionInboundData(
        remark=protocol,
        inbound_tag=f"{protocol}-inbound",
        protocol=protocol,
        address=IPV6,
        port=443,
        network="tcp",
        tls_config=TLSConfig(tls=None),
        transport_config=TCPTransportConfig(),
        priority=0,
    )


def _render(protocol: str, settings: dict) -> str:
    links = StandardLinks()
    links.add("r", IPV6, _inbound(protocol), settings)
    return links.links[0]


def test_vless_link_brackets_ipv6_address():
    link = _render("vless", {"id": USER_ID})
    assert f"@[{IPV6}]:443" in link


def test_trojan_link_brackets_ipv6_address():
    link = _render("trojan", {"password": "pw"})
    assert f"@[{IPV6}]:443" in link


def test_shadowsocks_link_brackets_ipv6_address():
    link = _render("shadowsocks", {"method": "aes-256-gcm", "password": "pw"})
    assert f"@[{IPV6}]:443" in link


def test_hysteria_link_brackets_ipv6_address():
    link = _render("hysteria", {"auth": "pw"})
    assert f"@[{IPV6}]:443" in link


def test_hysteria2_link_brackets_ipv6_address():
    link = _render("hysteria2", {"password": "pw"})
    assert f"@[{IPV6}]:443" in link


def test_ipv4_address_is_not_bracketed():
    links = StandardLinks()
    inbound = _inbound("vless").model_copy(update={"address": "1.2.3.4"})
    links.add("r", "1.2.3.4", inbound, {"id": USER_ID})
    assert "@1.2.3.4:443" in links.links[0]
    assert "[" not in links.links[0]


def test_wireguard_link_brackets_ipv6_address():
    links = StandardLinks()
    inbound = _inbound("wireguard").model_copy(
        update={"wireguard_public_key": "pub", "wireguard_local_address": ["10.0.0.2/32"]}
    )
    links.add("r", IPV6, inbound, {"private_key": "priv", "peer_ips": ["10.0.0.2/32"]})
    assert f"wireguard://priv@[{IPV6}]:443/" in links.links[0]
