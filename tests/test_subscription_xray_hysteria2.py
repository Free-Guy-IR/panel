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


def test_a_host_without_finalmask_renders_without_obfs_or_limits():
    conf = XrayConfiguration(xray_template_content=XRAY_TEMPLATE)
    inbound = _inbound("hysteria2", "tcp").model_copy(update={"finalmask": None})
    conf.add(remark="bare", address="histe.example.com", inbound=inbound, settings={"password": "p"})
    stream = json.loads(conf.render())[0]["outbounds"][0]["streamSettings"]
    assert stream["hysteriaSettings"] == {"version": 2, "auth": "p"}
    assert "finalmask" not in stream


def test_a_configured_bandwidth_limit_is_carried_through_untouched():
    conf = XrayConfiguration(xray_template_content=XRAY_TEMPLATE)
    inbound = _inbound("hysteria2", "tcp").model_copy(
        update={"finalmask": {"quicParams": {"brutalUp": "4 mbps", "brutalDown": "6 mbps"}}}
    )
    conf.add(remark="capped", address="histe.example.com", inbound=inbound, settings={"password": "p"})
    quic = json.loads(conf.render())[0]["outbounds"][0]["streamSettings"]["finalmask"]["quicParams"]
    assert quic == {"brutalUp": "4 mbps", "brutalDown": "6 mbps"}


def test_removing_the_limit_removes_quicparams_entirely():
    conf = XrayConfiguration(xray_template_content=XRAY_TEMPLATE)
    inbound = _inbound("hysteria2", "tcp").model_copy(
        update={"finalmask": {"udp": [{"type": "salamander", "settings": {"password": "obfs-pw"}}]}}
    )
    conf.add(remark="uncapped", address="histe.example.com", inbound=inbound, settings={"password": "p"})
    finalmask = json.loads(conf.render())[0]["outbounds"][0]["streamSettings"]["finalmask"]
    assert "quicParams" not in finalmask
    assert finalmask["udp"][0]["settings"]["password"] == "obfs-pw"


def test_tls_off_drops_tls_settings_but_keeps_the_auth():
    conf = XrayConfiguration(xray_template_content=XRAY_TEMPLATE)
    inbound = _inbound("hysteria2", "tcp").model_copy(update={"tls_config": TLSConfig(tls=None)})
    conf.add(remark="notls", address="histe.example.com", inbound=inbound, settings={"password": "p"})
    stream = json.loads(conf.render())[0]["outbounds"][0]["streamSettings"]
    assert stream["hysteriaSettings"]["auth"] == "p"
    assert stream.get("security") in (None, "none", "")


def test_a_distinct_sni_is_honoured():
    conf = XrayConfiguration(xray_template_content=XRAY_TEMPLATE)
    inbound = _inbound("hysteria2", "tcp").model_copy(
        update={"tls_config": TLSConfig(tls="tls", sni="cover.example.net")}
    )
    conf.add(remark="sni", address="real.example.com", inbound=inbound, settings={"password": "p"})
    outbound = json.loads(conf.render())[0]["outbounds"][0]
    assert outbound["settings"]["address"] == "real.example.com"
    assert outbound["streamSettings"]["tlsSettings"]["serverName"] == "cover.example.net"


def test_a_missing_password_is_tolerated_and_simply_omits_the_auth():
    conf = XrayConfiguration(xray_template_content=XRAY_TEMPLATE)
    conf.add(remark="nopass", address="histe.example.com", inbound=_inbound("hysteria2", "tcp"), settings={})
    stream = json.loads(conf.render())[0]["outbounds"][0]["streamSettings"]
    assert stream["hysteriaSettings"] == {"version": 2}


def test_the_legacy_hysteria_builder_still_requires_its_auth_key():
    conf = XrayConfiguration(xray_template_content=XRAY_TEMPLATE)
    with pytest.raises(KeyError):
        conf.add(remark="x", address="h.example.com", inbound=_inbound("hysteria", "hysteria"), settings={})
