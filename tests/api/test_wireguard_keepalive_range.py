from app.models.subscription import SubscriptionInboundData, TCPTransportConfig, TLSConfig
from app.subscription.wireguard import WireGuardConfiguration
from tests.api import client
from tests.api.helpers import auth_headers, create_core, delete_core, get_inbounds, unique_name

WIREGUARD_MAX_KEEPALIVE = 65535


def _post_host(access_token, keepalive_seconds):
    response = client.post(
        "/api/host",
        headers=auth_headers(access_token),
        json={
            "remark": unique_name("wg_keepalive"),
            "address": ["wg.example.com"],
            "port": 51820,
            "inbound_tag": get_inbounds(access_token)[0],
            "priority": 1,
            "wireguard_overrides": {"keepalive_seconds": keepalive_seconds},
        },
    )
    if response.status_code == 201:
        client.delete(f"/api/host/{response.json()['id']}", headers=auth_headers(access_token))
    return response


def test_host_api_refuses_a_keepalive_wireguard_cannot_take(access_token):
    core = create_core(access_token, name=unique_name("wg_keepalive_core"))
    try:
        for keepalive_seconds, expected in ((65536, 422), (86400, 422), (65535, 201), (25, 201), (0, 201)):
            response = _post_host(access_token, keepalive_seconds)
            assert response.status_code == expected, (keepalive_seconds, response.text)
    finally:
        delete_core(access_token, core["id"])


def _persistent_keepalive_lines(keepalive) -> list[str]:
    inbound = SubscriptionInboundData(
        remark="wg",
        inbound_tag="wg-in",
        protocol="wireguard",
        address="wg.example.com",
        port=51820,
        network="udp",
        tls_config=TLSConfig(),
        transport_config=TCPTransportConfig(path="", host=[]),
        mux_settings=None,
        wireguard_public_key="cHViLWtleS1wdWIta2V5LXB1Yi1rZXktcHViLWtleTA=",
        wireguard_allowed_ips=["0.0.0.0/0"],
        wireguard_keepalive=keepalive,
    )
    conf = WireGuardConfiguration()
    conf.add("wg", inbound.address, inbound, {"private_key": "cHJpdi1rZXk=", "peer_ips": ["10.0.0.2/32"]})
    rendered = "\n".join(content for _, content in conf.configs)
    return [line for line in rendered.splitlines() if line.startswith("PersistentKeepalive")]


def test_the_wireguard_file_never_carries_a_keepalive_outside_what_wg_accepts():
    for keepalive, expected in (
        (86400, ["PersistentKeepalive = 65535"]),
        (65536, ["PersistentKeepalive = 65535"]),
        (65535, ["PersistentKeepalive = 65535"]),
        (25, ["PersistentKeepalive = 25"]),
        (1, ["PersistentKeepalive = 1"]),
        (0, []),
        (-5, []),
        (None, []),
    ):
        assert _persistent_keepalive_lines(keepalive) == expected, keepalive
