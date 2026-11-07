import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.db.migrations.versions.e8c6a4f1d2b7_add_client_templates_table import DEFAULT_XRAY_SUBSCRIPTION_TEMPLATE
from app.models.subscription import SubscriptionInboundData, TCPTransportConfig, TLSConfig
from app.subscription.xray import XrayConfiguration

ENDPOINT_DNS_TAG = "wireguard-endpoint-dns"
ENDPOINT_DIRECT_TAG = "wireguard-endpoint-direct"
USER_ID = "11111111-1111-1111-1111-111111111111"
WG_SETTINGS = {"private_key": "client-priv", "peer_ips": ["10.66.0.2/32"]}


def _wireguard_inbound(
    address: str = "wg.example.com", port: int | str = 559, public_key: str = "server-pub"
) -> SubscriptionInboundData:
    return SubscriptionInboundData(
        remark="wg",
        inbound_tag="wg",
        protocol="wireguard",
        address=address,
        port=port,
        network="udp",
        tls_config=TLSConfig(),
        transport_config=TCPTransportConfig(),
        priority=0,
        wireguard_public_key=public_key,
    )


def _vless_inbound() -> SubscriptionInboundData:
    return SubscriptionInboundData(
        remark="v",
        inbound_tag="v",
        protocol="vless",
        address="vless.example.com",
        port=443,
        network="tcp",
        tls_config=TLSConfig(tls="tls", sni="vless.example.com"),
        transport_config=TCPTransportConfig(),
        priority=0,
    )


def _render_wireguard(template: dict | str = DEFAULT_XRAY_SUBSCRIPTION_TEMPLATE, address: str = "wg.example.com"):
    text = template if isinstance(template, str) else json.dumps(template)
    conf = XrayConfiguration(xray_template_content=text)
    conf.add(remark="🇹🇷| WireGuard", address=address, inbound=_wireguard_inbound(address), settings=WG_SETTINGS)
    return json.loads(conf.render())[0]


def _template(**overrides) -> dict:
    template = json.loads(DEFAULT_XRAY_SUBSCRIPTION_TEMPLATE)
    for key, value in overrides.items():
        if value is None:
            template.pop(key, None)
        else:
            template[key] = value
    return template


def _endpoint_servers(doc: dict) -> list:
    return [
        s for s in doc.get("dns", {}).get("servers", []) if isinstance(s, dict) and s.get("tag") == ENDPOINT_DNS_TAG
    ]


def _endpoint_clones(doc: dict, domain: str = "wg.example.com") -> list:
    return [
        s
        for s in doc.get("dns", {}).get("servers", [])
        if isinstance(s, dict) and s.get("domains") == [f"full:{domain}"] and s.get("skipFallback") is True
    ]


def test_a_domain_endpoint_is_resolved_by_dedicated_servers_that_only_answer_that_name():
    doc = _render_wireguard()
    assert _endpoint_servers(doc) == [
        {"address": "1.1.1.1", "domains": ["full:wg.example.com"], "skipFallback": True, "tag": ENDPOINT_DNS_TAG},
        {"address": "8.8.8.8", "domains": ["full:wg.example.com"], "skipFallback": True, "tag": ENDPOINT_DNS_TAG},
    ]


def test_the_template_servers_still_follow_the_endpoint_servers_untouched():
    doc = _render_wireguard()
    assert doc["dns"]["servers"][2:] == ["1.1.1.1", "8.8.8.8"]


def test_endpoint_lookups_are_routed_to_a_direct_outbound_before_any_template_rule():
    template = _template(
        routing={
            "domainStrategy": "AsIs",
            "rules": [{"type": "field", "ip": ["geoip:private"], "outboundTag": "BLOCK"}],
        }
    )
    doc = _render_wireguard(template)
    assert doc["routing"]["rules"][0] == {
        "type": "field",
        "inboundTag": [ENDPOINT_DNS_TAG],
        "outboundTag": ENDPOINT_DIRECT_TAG,
    }
    assert doc["routing"]["rules"][1:] == template["routing"]["rules"]
    assert doc["routing"]["domainStrategy"] == "AsIs"
    direct = [o for o in doc["outbounds"] if o.get("tag") == ENDPOINT_DIRECT_TAG]
    assert direct == [{"protocol": "freedom", "tag": ENDPOINT_DIRECT_TAG}]


def test_the_wireguard_outbound_stays_first_and_unchanged():
    doc = _render_wireguard()
    first = doc["outbounds"][0]
    assert first["protocol"] == "wireguard"
    assert first["tag"] == "proxy"
    assert first["settings"]["peers"][0]["endpoint"] == "wg.example.com:559"
    assert first["settings"]["domainStrategy"] == "ForceIP"
    assert [o["tag"] for o in doc["outbounds"][1:]] == ["DIRECT", "BLOCK", ENDPOINT_DIRECT_TAG]


@pytest.mark.parametrize("address", ["203.0.113.7", "2001:db8::7", "[2001:db8::7]"])
def test_an_ip_endpoint_gets_no_extra_dns_routing_or_outbound(address):
    template = json.loads(DEFAULT_XRAY_SUBSCRIPTION_TEMPLATE)
    doc = _render_wireguard(address=address)
    assert doc["dns"] == template["dns"]
    assert doc["routing"] == template["routing"]
    assert [o.get("tag") for o in doc["outbounds"]] == ["proxy", "DIRECT", "BLOCK"]


def test_other_protocols_are_untouched():
    template = json.loads(DEFAULT_XRAY_SUBSCRIPTION_TEMPLATE)
    conf = XrayConfiguration(xray_template_content=DEFAULT_XRAY_SUBSCRIPTION_TEMPLATE)
    conf.add(remark="v", address="vless.example.com", inbound=_vless_inbound(), settings={"id": USER_ID})
    doc = json.loads(conf.render())[0]
    assert doc["dns"] == template["dns"]
    assert doc["routing"] == template["routing"]
    assert [o.get("tag") for o in doc["outbounds"]] == ["proxy", "DIRECT", "BLOCK"]


def test_the_shared_template_is_not_mutated_across_configs():
    template = json.loads(DEFAULT_XRAY_SUBSCRIPTION_TEMPLATE)
    conf = XrayConfiguration(xray_template_content=DEFAULT_XRAY_SUBSCRIPTION_TEMPLATE)
    conf.add(remark="a", address="a.example.com", inbound=_wireguard_inbound("a.example.com"), settings=WG_SETTINGS)
    conf.add(remark="b", address="b.example.com", inbound=_wireguard_inbound("b.example.com"), settings=WG_SETTINGS)
    conf.add(remark="v", address="vless.example.com", inbound=_vless_inbound(), settings={"id": USER_ID})
    docs = json.loads(conf.render())
    assert [s["domains"] for s in _endpoint_servers(docs[0])] == [["full:a.example.com"]] * 2
    assert [s["domains"] for s in _endpoint_servers(docs[1])] == [["full:b.example.com"]] * 2
    assert len(docs[1]["routing"]["rules"]) == 1
    assert [o.get("tag") for o in docs[1]["outbounds"]].count(ENDPOINT_DIRECT_TAG) == 1
    assert docs[2]["dns"] == template["dns"]
    assert docs[2]["routing"] == template["routing"]
    assert conf.template == template


def test_a_template_without_dns_is_left_to_the_system_resolver():
    doc = _render_wireguard(_template(dns=None))
    assert "dns" not in doc
    assert doc["routing"] == json.loads(DEFAULT_XRAY_SUBSCRIPTION_TEMPLATE)["routing"]
    assert ENDPOINT_DIRECT_TAG not in [o.get("tag") for o in doc["outbounds"]]


def test_a_template_without_routing_gets_only_the_endpoint_rule():
    doc = _render_wireguard(_template(routing=None))
    assert doc["routing"] == {
        "rules": [{"type": "field", "inboundTag": [ENDPOINT_DNS_TAG], "outboundTag": ENDPOINT_DIRECT_TAG}]
    }


@pytest.mark.parametrize(
    "servers",
    [
        ["localhost"],
        ["fakedns"],
        ["localhost", "fakedns"],
        ["https+local://1.1.1.1/dns-query", "tcp+local://8.8.8.8", "quic+local://dns.adguard.com"],
        [{"address": "1.1.1.1", "skipFallback": True, "domains": ["geosite:ir"]}],
        [],
    ],
)
def test_servers_that_never_go_through_routing_need_no_bootstrap(servers):
    template = _template(dns={"servers": servers})
    doc = _render_wireguard(template)
    assert doc["dns"] == template["dns"]
    assert doc["routing"] == template["routing"]
    assert ENDPOINT_DIRECT_TAG not in [o.get("tag") for o in doc["outbounds"]]


def test_object_servers_keep_their_address_port_and_query_strategy_and_restricted_servers_are_skipped():
    servers = [
        {
            "address": "9.9.9.9",
            "port": 5353,
            "domains": ["geosite:google"],
            "expectIPs": ["geoip:us"],
            "queryStrategy": "UseIPv4",
        },
        {"address": "1.1.1.1", "skipFallback": True, "domains": ["geosite:ir"]},
        "https://dns.google/dns-query",
        "tcp://9.9.9.9",
        "fakedns",
    ]
    doc = _render_wireguard(_template(dns={"servers": servers, "queryStrategy": "UseIPv4"}))
    clones = [{k: v for k, v in s.items() if k not in ("domains", "skipFallback")} for s in _endpoint_clones(doc)]
    assert clones == [
        {"address": "9.9.9.9", "port": 5353, "queryStrategy": "UseIPv4", "tag": ENDPOINT_DNS_TAG},
        {"address": "https://dns.google/dns-query", "tag": ENDPOINT_DNS_TAG},
        {"address": "tcp://9.9.9.9", "tag": ENDPOINT_DNS_TAG},
    ]
    assert doc["dns"]["servers"][len(clones) :] == servers
    assert doc["dns"]["queryStrategy"] == "UseIPv4"


def test_local_resolvers_keep_their_place_in_the_endpoint_lookup_order_without_a_route():
    servers = ["localhost", "1.1.1.1", "https+local://dns.google/dns-query", "8.8.8.8"]
    doc = _render_wireguard(_template(dns={"servers": servers}))
    assert [(s["address"], s.get("tag")) for s in _endpoint_clones(doc)] == [
        ("localhost", None),
        ("1.1.1.1", ENDPOINT_DNS_TAG),
        ("https+local://dns.google/dns-query", None),
        ("8.8.8.8", ENDPOINT_DNS_TAG),
    ]
    assert doc["dns"]["servers"][4:] == servers


def test_a_wireguard_host_with_fragment_keeps_its_dialer_and_gets_the_bootstrap():
    inbound = _wireguard_inbound().model_copy(
        update={"fragment_settings": {"xray": {"packets": "1-3", "length": "10-20", "interval": "5-10"}}}
    )
    conf = XrayConfiguration(xray_template_content=DEFAULT_XRAY_SUBSCRIPTION_TEMPLATE)
    conf.add(remark="wg", address="wg.example.com", inbound=inbound, settings=WG_SETTINGS)
    doc = json.loads(conf.render())[0]
    assert [o.get("tag") for o in doc["outbounds"]] == ["proxy", "dialer", "DIRECT", "BLOCK", ENDPOINT_DIRECT_TAG]
    assert doc["outbounds"][0]["streamSettings"]["sockopt"]["dialerProxy"] == "dialer"
    assert len(_endpoint_servers(doc)) == 2


def test_an_existing_tag_of_the_same_name_is_not_duplicated():
    template = _template()
    template["outbounds"].append({"protocol": "freedom", "tag": ENDPOINT_DIRECT_TAG})
    doc = _render_wireguard(template)
    assert [o.get("tag") for o in doc["outbounds"]].count(ENDPOINT_DIRECT_TAG) == 1


def test_a_per_host_template_override_gets_the_same_bootstrap():
    override = json.dumps(_template(dns={"servers": ["9.9.9.9"]}))
    conf = XrayConfiguration(xray_template_content=DEFAULT_XRAY_SUBSCRIPTION_TEMPLATE)
    conf.add(
        remark="wg",
        address="wg.example.com",
        inbound=_wireguard_inbound(),
        settings=WG_SETTINGS,
        template_content=override,
    )
    doc = json.loads(conf.render())[0]
    assert [s["address"] for s in _endpoint_servers(doc)] == ["9.9.9.9"]
    assert doc["dns"]["servers"][-1] == "9.9.9.9"


def _xray_binary() -> str | None:
    candidate = os.environ.get("PASARGUARD_TEST_XRAY_BINARY") or shutil.which("xray")
    return candidate if candidate and Path(candidate).is_file() else None


@pytest.mark.skipif(_xray_binary() is None, reason="needs a real xray binary (PASARGUARD_TEST_XRAY_BINARY)")
def test_the_real_xray_core_accepts_the_generated_config(tmp_path):
    keys = []
    for _ in range(2):
        out = subprocess.run([_xray_binary(), "wg"], capture_output=True, text=True, timeout=30, check=True).stdout
        keys.append(dict(line.split(": ", 1) for line in out.strip().splitlines()))
    conf = XrayConfiguration(xray_template_content=DEFAULT_XRAY_SUBSCRIPTION_TEMPLATE)
    conf.add(
        remark="wg",
        address="wg.example.com",
        inbound=_wireguard_inbound(public_key=keys[0]["Password (PublicKey)"]),
        settings={"private_key": keys[1]["PrivateKey"], "peer_ips": ["10.66.0.2/32"]},
    )
    doc = json.loads(conf.render())[0]
    assert _endpoint_servers(doc)
    path = tmp_path / "client.json"
    path.write_text(json.dumps(doc))
    result = subprocess.run(
        [_xray_binary(), "run", "-test", "-c", str(path)], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Configuration OK" in result.stdout


def _template_with_own_wireguard_outbound() -> dict:
    template = _template()
    template["outbounds"].append(
        {
            "protocol": "wireguard",
            "tag": "warp",
            "settings": {
                "secretKey": "template-priv",
                "address": ["172.16.0.2/32"],
                "peers": [{"publicKey": "template-pub", "endpoint": "engage.cloudflareclient.com:2408"}],
            },
        }
    )
    return template


def test_a_template_wireguard_outbound_leaves_non_wireguard_hosts_untouched():
    template = _template_with_own_wireguard_outbound()
    conf = XrayConfiguration(xray_template_content=json.dumps(template))
    conf.add(remark="v", address="vless.example.com", inbound=_vless_inbound(), settings={"id": USER_ID})
    doc = json.loads(conf.render())[0]
    assert doc["dns"] == template["dns"]
    assert doc["routing"] == template["routing"]
    assert [o.get("tag") for o in doc["outbounds"]] == ["proxy", "DIRECT", "BLOCK", "warp"]


def test_a_template_wireguard_outbound_keeps_its_endpoint_lookup_on_the_template_route():
    doc = _render_wireguard(_template_with_own_wireguard_outbound())
    clones = _endpoint_clones(doc)
    assert [s["domains"] for s in clones] == [["full:wg.example.com"]] * 2
    assert not _endpoint_clones(doc, "engage.cloudflareclient.com")
