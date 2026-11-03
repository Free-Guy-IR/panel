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
