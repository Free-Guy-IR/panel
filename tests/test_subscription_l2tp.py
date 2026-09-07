import json

from app.models.subscription import SubscriptionInboundData, TCPTransportConfig, TLSConfig
from app.subscription.l2tp import L2TPConfiguration


def _inbound(**overrides):
    base = {
        "remark": "Germany L2TP",
        "inbound_tag": "l2tp-main",
        "protocol": "l2tp",
        "address": "203.0.113.10",
        "port": 1701,
        "network": "udp",
        "tls_config": TLSConfig(),
        "transport_config": TCPTransportConfig(path="", host=[]),
        "finalmask": {"l2tp": {"server_addr": "vpn.example.com", "psk": "correct-horse-battery", "dns": ["1.1.1.1"]}},
    }
    base.update(overrides)
    return SubscriptionInboundData(**base)


def test_render_is_empty_json_list_without_hosts():
    assert json.loads(L2TPConfiguration().render()) == []


def test_add_builds_credentials_from_finalmask_and_user_settings():
    conf = L2TPConfiguration()
    conf.add("Germany L2TP", "203.0.113.10", _inbound(), {"_user_id": 42, "password": "pw123"})
    details = json.loads(conf.render())
    assert details == [
        {
            "remark": "Germany L2TP",
            "server": "203.0.113.10",
            "username": "42",
            "password": "pw123",
            "secret": "correct-horse-battery",
            "dns": ["1.1.1.1"],
        }
    ]


def test_missing_password_or_psk_yields_nothing():
    conf = L2TPConfiguration()
    conf.add("x", "203.0.113.10", _inbound(), {"_user_id": 42, "password": None})
    conf.add(
        "x", "203.0.113.10", _inbound(finalmask={"l2tp": {"server_addr": "h"}}), {"_user_id": 42, "password": "pw"}
    )
    conf.add("x", "203.0.113.10", _inbound(protocol="vless"), {"_user_id": 42, "password": "pw"})
    assert conf.details == []


def test_empty_host_address_falls_back_to_core_server_addr():
    conf = L2TPConfiguration()
    conf.add("x", "", _inbound(), {"_user_id": 7, "password": "pw"})
    assert conf.details[0]["server"] == "vpn.example.com"


def test_duplicate_remarks_are_disambiguated():
    conf = L2TPConfiguration()
    conf.add("same", "203.0.113.10", _inbound(), {"_user_id": 7, "password": "pw"})
    conf.add("same", "203.0.113.11", _inbound(), {"_user_id": 7, "password": "pw"})
    assert [d["remark"] for d in conf.details] == ["same", "same (2)"]
