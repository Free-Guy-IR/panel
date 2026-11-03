import json

from app.core.hosts import _normalize_finalmask_link


def test_finalmask_link_supports_stable_fragment_fields_without_mutating_input():
    settings = {"tcp": [{"type": "fragment", "settings": {"lengths": ["100-200"], "delays": ["10-20"]}}]}

    result = json.loads(_normalize_finalmask_link(settings))

    assert result["tcp"][0]["settings"] == {
        "lengths": ["100-200"],
        "delays": ["10-20"],
        "length": "100-200",
        "delay": "10-20",
    }
    assert "length" not in settings["tcp"][0]["settings"]


def test_finalmask_link_normalizes_string_payload():
    payload = '{"tcp":[{"type":"fragment","settings":{"lengths":["100-200"],"delays":["10-20"]}}]}'

    result = json.loads(_normalize_finalmask_link(payload))

    assert result["tcp"][0]["settings"]["length"] == "100-200"
    assert result["tcp"][0]["settings"]["delay"] == "10-20"


def _xdns_finalmask(addr: str) -> dict:
    return {
        "udp": [
            {
                "type": "xdns",
                "settings": {"domains": [{"names": ["t.example.com"]}], "resolvers": [{"addrs": [addr]}]},
            }
        ]
    }


def test_finalmask_link_keeps_doh_resolver_tls_config():
    for source in (
        _xdns_finalmask("https://dns.example/dns-query"),
        json.dumps(_xdns_finalmask("https://dns.example/dns-query")),
    ):
        result = json.loads(_normalize_finalmask_link(source))

        assert result["udp"][0]["settings"]["resolvers"] == [
            {"addrs": ["https://dns.example/dns-query"], "tlsConfig": {}}
        ]


def test_finalmask_link_keeps_no_tls_config_on_plain_resolver():
    result = json.loads(_normalize_finalmask_link(_xdns_finalmask("udp://1.1.1.1")))

    assert result["udp"][0]["settings"]["resolvers"] == [{"addrs": ["udp://1.1.1.1"]}]


def test_finalmask_link_does_not_mutate_doh_input():
    settings = _xdns_finalmask("https://dns.example/dns-query")

    _normalize_finalmask_link(settings)

    assert settings == _xdns_finalmask("https://dns.example/dns-query")


def test_xhttp_share_link_extra_download_settings_keeps_doh_tls_config():
    from app.models.subscription import XHTTPTransportConfig
    from app.subscription.links import StandardLinks

    download = {"address": "d.example.com", "finalmask": _xdns_finalmask("https://dns.example/dns-query")}
    payload = {}

    StandardLinks()._transport_xhttp(
        payload, "vless", XHTTPTransportConfig(path="/x", download_settings=download), "/x"
    )

    extra = json.loads(payload["extra"])
    assert extra["downloadSettings"]["finalmask"]["udp"][0]["settings"]["resolvers"] == [
        {"addrs": ["https://dns.example/dns-query"], "tlsConfig": {}}
    ]
    assert download["finalmask"] == _xdns_finalmask("https://dns.example/dns-query")
