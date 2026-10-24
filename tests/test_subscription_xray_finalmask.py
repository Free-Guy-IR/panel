import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.models.host import FinalMask
from app.subscription.xray import XrayConfiguration


def test_xray_finalmask_serializes_raw_max_split_with_xray_alias():
    stream_settings = XrayConfiguration._stream_setting_config(
        finalmask={
            "tcp": [
                {
                    "type": "fragment",
                    "settings": {
                        "packets": "tlshello",
                        "max_split": "3-6",
                    },
                }
            ]
        }
    )

    settings = stream_settings["finalmask"]["tcp"][0]["settings"]

    assert settings["maxSplit"] == "3-6"
    assert "max_split" not in settings


def test_xray_finalmask_keeps_unrecognized_raw_values_unchanged():
    finalmask = {"tcp": [{"type": "future-type", "settings": {"max_split": "3-6"}}]}

    stream_settings = XrayConfiguration._stream_setting_config(finalmask=finalmask)

    assert stream_settings["finalmask"] == finalmask


@pytest.mark.parametrize("as_model", [False, True])
def test_xray_finalmask_combines_stable_fragment_fields_and_max_split_alias(as_model):
    finalmask = {
        "tcp": [
            {
                "type": "fragment",
                "settings": {
                    "packets": "tlshello",
                    "lengths": ["100-200", "200-300"],
                    "delays": ["10-20", "20-30"],
                    "max_split": "3-6",
                },
            }
        ]
    }
    original = deepcopy(finalmask)
    value = FinalMask.model_validate(finalmask) if as_model else finalmask

    stream_settings = XrayConfiguration._stream_setting_config(finalmask=value)
    settings = stream_settings["finalmask"]["tcp"][0]["settings"]

    assert settings["length"] == "100-200"
    assert settings["delay"] == "10-20"
    assert settings["lengths"] == ["100-200", "200-300"]
    assert settings["delays"] == ["10-20", "20-30"]
    assert settings["maxSplit"] == "3-6"
    assert "max_split" not in settings
    assert finalmask == original


def test_xray_finalmask_header_custom_items_use_xray_types():
    finalmask = {
        "udp": [
            {
                "type": "header-custom",
                "settings": {
                    "client": [{"type": "array", "rand": "32", "delay": "5-10", "randRange": "32"}],
                    "server": [{"type": "array", "rand": "", "packet": "abc"}],
                },
            }
        ],
        "tcp": [
            {
                "type": "header-custom",
                "settings": {
                    "clients": [[{"type": "array", "rand": "7", "delay": "10", "randRange": "1-9"}]],
                    "servers": [[{"type": "array", "delay": "5-10", "rand_range": "0-255"}]],
                },
            }
        ],
    }

    stream_settings = XrayConfiguration._stream_setting_config(finalmask=finalmask)
    mask = stream_settings["finalmask"]

    assert mask["udp"][0]["settings"] == {
        "client": [{"type": "array", "rand": 32, "randRange": "32"}],
        "server": [{"type": "array", "packet": "abc"}],
    }
    assert mask["tcp"][0]["settings"] == {
        "clients": [[{"type": "array", "rand": 7, "delay": 10, "randRange": "1-9"}]],
        "servers": [[{"type": "array", "delay": "5-10", "randRange": "0-255"}]],
    }


def test_xray_finalmask_noise_keeps_range_strings():
    noise = {"type": "array", "rand": "1-8192", "delay": "5-10", "randRange": "80-120"}
    finalmask = {"udp": [{"type": "noise", "settings": {"noise": [noise]}}]}

    stream_settings = XrayConfiguration._stream_setting_config(finalmask=finalmask)

    assert stream_settings["finalmask"]["udp"][0]["settings"]["noise"] == [noise]


def test_xray_finalmask_legacy_snake_keys_are_emitted_as_xray_keys():
    finalmask = {
        "udp": [
            {
                "type": "noise",
                "settings": {"noise": [{"type": "array", "delay": "5-10", "rand_range": "80-120"}]},
            }
        ]
    }

    stream_settings = XrayConfiguration._stream_setting_config(finalmask=finalmask)

    assert stream_settings["finalmask"]["udp"][0]["settings"]["noise"] == [
        {"type": "array", "delay": "5-10", "randRange": "80-120"}
    ]


@pytest.mark.parametrize("rand", ["abc", "1-8", "3.5", -1])
def test_finalmask_header_custom_rejects_invalid_rand(rand):
    mask = {"udp": [{"type": "header-custom", "settings": {"client": [{"type": "array", "rand": rand}]}}]}

    with pytest.raises(ValidationError):
        FinalMask.model_validate(mask)


def test_xray_finalmask_keeps_udphop_with_xray_field_names():
    udphop = {
        "type": "udphop",
        "settings": {
            "mode": "intervalRemote",
            "interval": "10-60",
            "remoteIPs": ["203.0.113.0/24"],
            "remotePorts": "20000-20010",
        },
    }
    finalmask = FinalMask.model_validate({"udp": [udphop]}).model_dump(by_alias=True, exclude_none=True, mode="json")

    stream_settings = XrayConfiguration._stream_setting_config(finalmask=finalmask)

    assert stream_settings["finalmask"]["udp"] == [udphop]


def test_xray_finalmask_xdns_doh_resolvers_keep_xray_fields():
    settings = {
        "domains": [{"names": ["t.example.com"], "types": [1, 28]}],
        "resolvers": [
            {"addrs": ["https://dns.example/dns-query"], "worker": 8},
            {
                "addrs": ["https://dns3.example/dns-query"],
                "tlsConfig": {"alpn": ["h3"], "serverName": "front.example"},
                "quicConfig": {"congestion": "bbr"},
            },
        ],
        "extraPoll": 2,
    }
    finalmask = FinalMask.model_validate({"udp": [{"type": "xdns", "settings": settings}]})
    actual = XrayConfiguration._stream_setting_config(finalmask=finalmask)["finalmask"]["udp"][0]["settings"]

    assert actual == {
        **settings,
        "resolvers": [{**settings["resolvers"][0], "tlsConfig": {}}, settings["resolvers"][1]],
    }


def test_xray_finalmask_xdns_legacy_lists_are_upgraded():
    finalmask = FinalMask.model_validate(
        {"udp": [{"type": "xdns", "settings": {"domains": ["t.example.com"], "resolvers": ["udp://1.1.1.1"]}}]}
    )

    settings = finalmask.model_dump(by_alias=True, exclude_none=True, mode="json")["udp"][0]["settings"]
    assert settings == {"domains": [{"names": ["t.example.com"]}], "resolvers": [{"addrs": ["udp://1.1.1.1"]}]}


def test_xray_finalmask_header_dns_keeps_domain():
    finalmask = FinalMask.model_validate({"udp": [{"type": "header-dns", "settings": {"domain": "example.com"}}]})

    settings = finalmask.model_dump(by_alias=True, exclude_none=True, mode="json")["udp"][0]["settings"]
    assert settings == {"domain": "example.com"}


def test_xray_finalmask_xicmp_uses_new_config_and_discards_old_fields():
    finalmask = FinalMask.model_validate(
        {
            "udp": [
                {
                    "type": "xicmp",
                    "settings": {
                        "dgram": True,
                        "ips": ["192.0.2.1", "2001:db8::1"],
                        "listenIp": "0.0.0.0",
                        "id": 1234,
                    },
                }
            ]
        }
    )

    settings = XrayConfiguration._stream_setting_config(finalmask=finalmask)["finalmask"]["udp"][0]["settings"]
    assert settings == {"dgram": True, "ips": ["192.0.2.1", "2001:db8::1"]}


@pytest.mark.parametrize("ip", ["example.com", "192.0.2.999", "192.0.2.0/24"])
def test_xray_finalmask_xicmp_rejects_non_ip_addresses(ip):
    with pytest.raises(ValidationError, match="ips"):
        FinalMask.model_validate({"udp": [{"type": "xicmp", "settings": {"ips": [ip]}}]})


def _noise_exp_mask(**item):
    return {"udp": [{"type": "noise", "settings": {"noise": [{"type": "exp", **item}]}}]}


def test_xray_finalmask_keeps_noise_exp_template():
    exp = {"packet": "<b 0d0a0d0a><t><r 24>", "delay": "1-3"}
    finalmask = FinalMask.model_validate(_noise_exp_mask(**exp)).model_dump(
        by_alias=True, exclude_none=True, mode="json"
    )

    stream_settings = XrayConfiguration._stream_setting_config(finalmask=finalmask)

    assert stream_settings["finalmask"]["udp"][0]["settings"]["noise"] == [{"type": "exp", **exp}]


@pytest.mark.parametrize(
    "item",
    [{"packet": [1, 2]}, {"packet": ""}, {"packet": "   "}, {"packet": None}, {}],
    ids=["array", "empty", "blank", "null", "missing"],
)
def test_finalmask_noise_exp_rejects_bad_packet(item):
    with pytest.raises(ValidationError, match="exp noise needs a non-empty packet template string"):
        FinalMask.model_validate(_noise_exp_mask(**item))


@pytest.mark.parametrize(
    ("mask", "match"),
    [
        (
            {"udp": [{"type": "header-custom", "settings": {"client": [{"type": "exp", "packet": "<t>"}]}}]},
            r"client\.0\.type",
        ),
        (
            {"tcp": [{"type": "header-custom", "settings": {"clients": [[{"type": "exp", "packet": "<t>"}]]}}]},
            r"clients\.0\.0\.type",
        ),
    ],
    ids=["udp", "tcp"],
)
def test_finalmask_header_custom_rejects_exp(mask, match):
    with pytest.raises(ValidationError, match=match):
        FinalMask.model_validate(mask)


@pytest.mark.parametrize(
    "resolver",
    [
        {"addrs": ["https://dns.example/dns-query"]},
        {"addrs": ["https://dns.example/dns-query"], "tlsConfig": {}},
        {"addrs": ["https://dns.example/dns-query"], "tlsConfig": {"serverName": "", "alpn": []}},
    ],
)
def test_xray_finalmask_xdns_doh_resolver_json_keeps_empty_tls_config(resolver):
    finalmask = {
        "udp": [{"type": "xdns", "settings": {"domains": [{"names": ["t.example.com"]}], "resolvers": [resolver]}}]
    }

    for source in (finalmask, FinalMask.model_validate(finalmask)):
        stream_settings = XrayConfiguration._stream_setting_config(finalmask=deepcopy(source))
        generated = json.dumps(stream_settings)

        assert '"tlsConfig": {}' in generated
        assert stream_settings["finalmask"]["udp"][0]["settings"]["resolvers"] == [
            {"addrs": ["https://dns.example/dns-query"], "tlsConfig": {}}
        ]


def test_xray_finalmask_xdns_plain_resolver_gets_no_tls_config():
    finalmask = {
        "udp": [
            {
                "type": "xdns",
                "settings": {
                    "domains": [{"names": ["t.example.com"]}],
                    "resolvers": [{"addrs": ["udp://1.1.1.1"], "tlsConfig": {}, "quicConfig": {}}],
                },
            }
        ]
    }

    stream_settings = XrayConfiguration._stream_setting_config(finalmask=finalmask)

    assert stream_settings["finalmask"]["udp"][0]["settings"]["resolvers"] == [{"addrs": ["udp://1.1.1.1"]}]


def test_xray_finalmask_xdns_doh_tls_config_survives_outbound_normalization():
    finalmask = {
        "udp": [
            {
                "type": "xdns",
                "settings": {
                    "domains": [{"names": ["t.example.com"]}],
                    "resolvers": [{"addrs": ["https://d.example/q"]}],
                },
            }
        ]
    }
    stream_settings = XrayConfiguration._stream_setting_config(finalmask=finalmask)
    outbound = {"protocol": "vless", "settings": {}, "streamSettings": stream_settings}
    download = {"address": "a.example", "finalmask": stream_settings["finalmask"]}

    normalized = XrayConfiguration._normalize_and_remove_none_values(outbound)
    normalized_download = XrayConfiguration._normalize_and_remove_none_values(download)

    assert '"tlsConfig": {}' in json.dumps(normalized)
    assert normalized["streamSettings"]["finalmask"]["udp"][0]["settings"]["resolvers"] == [
        {"addrs": ["https://d.example/q"], "tlsConfig": {}}
    ]
    assert normalized_download["finalmask"]["udp"][0]["settings"]["resolvers"][0]["tlsConfig"] == {}


def test_restore_doh_tls_configs_matches_upstream_lowercase_https_check():
    from app.fork.finalmask import restore_doh_tls_configs

    upper = {"udp": [{"type": "xdns", "settings": {"resolvers": [{"addrs": ["HTTPS://dns.example/q"]}]}}]}
    lower = {"udp": [{"type": "xdns", "settings": {"resolvers": [{"addrs": ["https://dns.example/q"]}]}}]}

    assert "tlsConfig" not in restore_doh_tls_configs(upper)["udp"][0]["settings"]["resolvers"][0]
    assert restore_doh_tls_configs(lower)["udp"][0]["settings"]["resolvers"][0]["tlsConfig"] == {}
    model_resolver = FinalMask.model_validate(upper).udp[0].settings.resolvers[0]
    assert model_resolver.tls_config is None


def _xdns_inbound(finalmask: dict, network: str = "tcp", download=None):
    from app.models.subscription import SubscriptionInboundData, TCPTransportConfig, TLSConfig, XHTTPTransportConfig

    transport = (
        XHTTPTransportConfig(path="/x", download_settings=download) if network == "xhttp" else TCPTransportConfig()
    )
    return SubscriptionInboundData(
        remark="t",
        inbound_tag="T",
        protocol="vless",
        address="v.example.com",
        port=443,
        network=network,
        tls_config=TLSConfig(tls="tls", sni="v.example.com"),
        transport_config=transport,
        finalmask=finalmask,
        priority=0,
    )


@pytest.mark.parametrize("as_model", [False, True])
def test_xray_full_outbound_render_keeps_doh_resolver_tls_config(as_model):
    finalmask = {
        "udp": [
            {
                "type": "xdns",
                "settings": {
                    "domains": [{"names": ["t.example.com"]}],
                    "resolvers": [{"addrs": ["https://d.example/q"]}, {"addrs": ["udp://1.1.1.1"]}],
                },
            }
        ]
    }
    source = FinalMask.model_validate(finalmask) if as_model else deepcopy(finalmask)
    conf = XrayConfiguration(
        xray_template_content=json.dumps({"outbounds": [{"tag": "DIRECT", "protocol": "freedom"}]})
    )

    conf.add(
        remark="r",
        address="v.example.com",
        inbound=_xdns_inbound(source),
        settings={"id": "11111111-1111-1111-1111-111111111111", "flow": ""},
    )
    rendered = json.loads(conf.render())
    outbound = rendered[0]["outbounds"][0]

    assert outbound["protocol"] == "vless"
    assert outbound["streamSettings"]["finalmask"]["udp"][0]["settings"]["resolvers"] == [
        {"addrs": ["https://d.example/q"], "tlsConfig": {}},
        {"addrs": ["udp://1.1.1.1"]},
    ]
    assert finalmask["udp"][0]["settings"]["resolvers"][0] == {"addrs": ["https://d.example/q"]}


def test_xray_full_outbound_render_keeps_doh_tls_config_in_xhttp_download_settings():
    finalmask = {
        "udp": [
            {
                "type": "xdns",
                "settings": {
                    "domains": [{"names": ["t.example.com"]}],
                    "resolvers": [{"addrs": ["https://d.example/q"]}],
                },
            }
        ]
    }
    download = _xdns_inbound(deepcopy(finalmask), network="xhttp")
    conf = XrayConfiguration(
        xray_template_content=json.dumps({"outbounds": [{"tag": "DIRECT", "protocol": "freedom"}]})
    )

    conf.add(
        remark="r",
        address="v.example.com",
        inbound=_xdns_inbound(deepcopy(finalmask), network="xhttp", download=download),
        settings={"id": "11111111-1111-1111-1111-111111111111", "flow": ""},
    )
    stream = json.loads(conf.render())[0]["outbounds"][0]["streamSettings"]
    download_stream = stream["xhttpSettings"]["extra"]["downloadSettings"]

    assert stream["finalmask"]["udp"][0]["settings"]["resolvers"][0]["tlsConfig"] == {}
    assert download_stream["finalmask"]["udp"][0]["settings"]["resolvers"][0]["tlsConfig"] == {}
