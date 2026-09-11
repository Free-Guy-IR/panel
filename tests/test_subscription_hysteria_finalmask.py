from app.models.host import FinalMask
from app.models.subscription import SubscriptionInboundData, TCPTransportConfig, TLSConfig
from app.subscription.clash import ClashMetaConfiguration
from app.subscription.links import StandardLinks
from app.subscription.singbox import SingBoxConfiguration

FINALMASK_DICT = {
    "udp": [{"type": "salamander", "settings": {"password": "obfs-pw"}}],
    "quicParams": {"brutalUp": "50 mbps", "brutalDown": "100 mbps"},
}


def _model_finalmask() -> FinalMask:
    return FinalMask.model_validate(FINALMASK_DICT)


def _inbound(protocol: str, finalmask) -> SubscriptionInboundData:
    return SubscriptionInboundData(
        remark="t",
        inbound_tag="T",
        protocol=protocol,
        address="edge.example.com",
        port=443,
        network="tcp",
        tls_config=TLSConfig(tls="tls", sni="cert.example.com"),
        transport_config=TCPTransportConfig(),
        finalmask=finalmask,
        priority=0,
    )


def test_links_hysteria2_host_with_finalmask_model_renders_obfs():
    links = StandardLinks()
    links.add("hy2", "edge.example.com", _inbound("hysteria2", _model_finalmask()), {"password": "SECRET"})

    rendered = links.render()
    assert "obfs=salamander" in rendered
    assert "obfs-password=obfs-pw" in rendered


def test_links_hysteria_host_with_finalmask_model_renders_obfs():
    links = StandardLinks()
    links.add("hy", "edge.example.com", _inbound("hysteria", _model_finalmask()), {"auth": "SECRET"})

    rendered = links.render()
    assert "obfs-password=obfs-pw" in rendered


def test_clash_meta_hysteria2_host_with_finalmask_model_renders_obfs():
    meta = ClashMetaConfiguration()
    meta.add("hy2", "edge.example.com", _inbound("hysteria2", _model_finalmask()), {"password": "SECRET"})

    node = meta.data["proxies"][0]
    assert node["type"] == "hysteria2"
    assert node["obfs"] == "salamander"
    assert node["obfs-password"] == "obfs-pw"
    assert node["up"].lower() == "50 mbps"
    assert node["down"].lower() == "100 mbps"


def test_singbox_hysteria2_host_with_finalmask_model_renders_obfs():
    conf = SingBoxConfiguration()
    conf.add("hy2", "edge.example.com", _inbound("hysteria2", _model_finalmask()), {"password": "SECRET"})

    outbound = conf.config["outbounds"][0]
    assert outbound["type"] == "hysteria2"
    assert outbound["obfs"] == {"type": "salamander", "password": "obfs-pw"}


def test_hysteria2_host_with_plain_dict_finalmask_still_renders():
    links = StandardLinks()
    links.add("hy2", "edge.example.com", _inbound("hysteria2", dict(FINALMASK_DICT)), {"password": "SECRET"})

    assert "obfs-password=obfs-pw" in links.render()
