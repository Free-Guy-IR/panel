import json

from app.models.subscription import SubscriptionInboundData, TCPTransportConfig, TLSConfig
from app.subscription.xray import XrayConfiguration

XRAY_TEMPLATE = json.dumps({"log": {}, "inbounds": [], "outbounds": [{"tag": "DIRECT", "protocol": "freedom"}]})
SINGBOX_TEMPLATE = json.dumps(
    {
        "log": {},
        "outbounds": [
            {"tag": "proxy", "type": "selector", "outbounds": []},
            {"tag": "Best Latency", "type": "urltest", "outbounds": []},
            {"tag": "direct", "type": "direct"},
        ],
        "route": {},
    }
)
USER_ID = "11111111-1111-1111-1111-111111111111"


def _hysteria2_inbound() -> SubscriptionInboundData:
    return SubscriptionInboundData(
        remark="hy2",
        inbound_tag="Hysteria2",
        protocol="hysteria2",
        address="histe.example.com",
        port=445,
        network="udp",
        tls_config=TLSConfig(tls="tls", sni="histe.example.com"),
        transport_config=TCPTransportConfig(),
        finalmask={
            "udp": [{"type": "salamander", "settings": {"password": "obfs-secret"}}],
            "quicParams": {"brutalUp": "50 mbps", "brutalDown": "100 mbps"},
        },
        priority=0,
    )


def _vless_inbound() -> SubscriptionInboundData:
    return SubscriptionInboundData(
        remark="vl",
        inbound_tag="vless-tcp",
        protocol="vless",
        address="edge.example.com",
        port=443,
        network="tcp",
        tls_config=TLSConfig(tls="tls", sni="edge.example.com"),
        transport_config=TCPTransportConfig(),
        priority=0,
    )


def _render(*adds, singbox_template=SINGBOX_TEMPLATE):
    conf = XrayConfiguration(xray_template_content=XRAY_TEMPLATE, singbox_template_content=singbox_template)
    for remark, inbound, settings in adds:
        conf.add(remark=remark, address=inbound.address, inbound=inbound, settings=settings)
    return json.loads(conf.render())


def test_hysteria2_is_no_longer_dropped_from_the_xray_json_array():
    docs = _render(("🇩🇪 Germany ⭐", _hysteria2_inbound(), {"password": "user-pass"}))
    assert len(docs) == 1
    doc = docs[0]
    assert doc["remarks"] == "🇩🇪 Germany ⭐"
    hy = [o for o in doc["outbounds"] if o.get("type") == "hysteria2"]
    assert len(hy) == 1
    assert hy[0]["server"] == "histe.example.com"
    assert hy[0]["server_port"] == 445
    assert hy[0]["password"] == "user-pass"
    assert hy[0]["obfs"] == {"type": "salamander", "password": "obfs-secret"}
    assert hy[0]["tls"]["server_name"] == "histe.example.com"
    assert hy[0]["up_mbps"] == 50 and hy[0]["down_mbps"] == 100
    assert hy[0]["tag"] == "🇩🇪 Germany ⭐"


def test_the_embedded_document_is_a_complete_singbox_config_not_an_xray_one():
    doc = _render(("hy", _hysteria2_inbound(), {"password": "p"}))[0]
    assert "route" in doc and "log" in doc
    assert all("protocol" not in o for o in doc["outbounds"])
    selector = next(o for o in doc["outbounds"] if o.get("type") == "selector")
    urltest = next(o for o in doc["outbounds"] if o.get("type") == "urltest")
    assert "hy" in selector["outbounds"]
    assert urltest["outbounds"] == ["hy"]


def test_xray_protocols_in_the_same_subscription_are_untouched():
    docs = _render(
        ("vl", _vless_inbound(), {"id": USER_ID}),
        ("hy", _hysteria2_inbound(), {"password": "p"}),
    )
    assert [d["remarks"] for d in docs] == ["vl", "hy"]
    vless_doc = docs[0]
    assert vless_doc["outbounds"][0]["protocol"] == "vless"
    assert vless_doc["outbounds"][-1]["tag"] == "DIRECT"
    assert all("type" not in o for o in vless_doc["outbounds"])


def test_missing_singbox_template_still_emits_the_hysteria2_document():
    doc = _render(("hy", _hysteria2_inbound(), {"password": "p"}), singbox_template=None)[0]
    assert doc["remarks"] == "hy"
    assert any(o.get("type") == "hysteria2" for o in doc["outbounds"])


def test_legacy_hysteria_still_renders_as_an_xray_outbound():
    legacy = _hysteria2_inbound().model_copy(update={"protocol": "hysteria", "inbound_tag": "Hysteria"})
    doc = _render(("h1", legacy, {"auth": "auth-pass"}))[0]
    assert doc["outbounds"][0]["protocol"] == "hysteria"
