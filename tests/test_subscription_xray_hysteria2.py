import json

import pytest

from app.models.subscription import SubscriptionInboundData, TCPTransportConfig, TLSConfig
from app.subscription.xray import XrayConfiguration

XRAY_TEMPLATE = json.dumps({"outbounds": [{"tag": "DIRECT", "protocol": "freedom"}]})
USER_ID = "11111111-1111-1111-1111-111111111111"
FINALMASK = {
    "udp": [{"type": "salamander", "settings": {"password": "obfs-pw"}}],
    "quicParams": {"brutalUp": "50 mbps", "brutalDown": "100 mbps"},
}


def _inbound(protocol: str, network: str) -> SubscriptionInboundData:
    return SubscriptionInboundData(
        remark="t",
        inbound_tag="T",
        protocol=protocol,
        address="histe.example.com",
        port=445,
        network=network,
        tls_config=TLSConfig(tls="tls", sni="histe.example.com"),
        transport_config=TCPTransportConfig(),
        finalmask=FINALMASK,
        priority=0,
    )


def _render(protocol: str, network: str, settings: dict, remark: str = "r"):
    conf = XrayConfiguration(xray_template_content=XRAY_TEMPLATE)
    conf.add(remark=remark, address="histe.example.com", inbound=_inbound(protocol, network), settings=settings)
    return json.loads(conf.render())


def test_hysteria2_renders_identically_to_the_working_xray_core_hysteria_host():
    xray_core = _render("hysteria", "hysteria", {"auth": "SECRET"})
    singbox_core = _render("hysteria2", "tcp", {"password": "SECRET"})
    assert singbox_core == xray_core
    assert singbox_core[0]["outbounds"][0]["protocol"] == "hysteria"


def test_the_hysteria2_outbound_is_schema_pure_xray():
    outbound = _render("hysteria2", "tcp", {"password": "SECRET"})[0]["outbounds"][0]
    assert "protocol" in outbound and "type" not in outbound
    assert outbound["settings"]["version"] == 2
    assert outbound["settings"]["address"] == "histe.example.com"
    assert outbound["settings"]["port"] == 445
    stream = outbound["streamSettings"]
    assert stream["network"] == "hysteria"
    assert stream["hysteriaSettings"] == {"version": 2, "auth": "SECRET"}
    assert stream["tlsSettings"]["serverName"] == "histe.example.com"
    assert stream["finalmask"]["udp"][0]["settings"]["password"] == "obfs-pw"


def test_the_user_password_becomes_the_hysteria_auth():
    outbound = _render("hysteria2", "tcp", {"password": "per-user-pass"})[0]["outbounds"][0]
    assert outbound["streamSettings"]["hysteriaSettings"]["auth"] == "per-user-pass"


@pytest.mark.parametrize("network", ["tcp", "udp", "hysteria", ""])
def test_the_inbound_network_never_leaks_into_a_hysteria2_config(network):
    outbound = _render("hysteria2", network, {"password": "p"})[0]["outbounds"][0]
    assert outbound["streamSettings"]["network"] == "hysteria"


def test_the_host_remark_and_template_outbounds_survive():
    doc = _render("hysteria2", "tcp", {"password": "p"}, remark="🇩🇪 Germany ⭐")[0]
    assert doc["remarks"] == "🇩🇪 Germany ⭐"
    assert doc["outbounds"][-1]["tag"] == "DIRECT"


def test_legacy_hysteria_hosts_are_unchanged():
    outbound = _render("hysteria", "hysteria", {"auth": "legacy"})[0]["outbounds"][0]
    assert outbound["streamSettings"]["hysteriaSettings"]["auth"] == "legacy"


def test_other_protocols_are_untouched():
    outbound = _render("vless", "tcp", {"id": USER_ID})[0]["outbounds"][0]
    assert outbound["protocol"] == "vless"
    assert outbound["settings"]["vnext"][0]["users"][0]["id"] == USER_ID
