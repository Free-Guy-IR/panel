import io
import zipfile

from app.models.subscription import SubscriptionInboundData, TCPTransportConfig, TLSConfig
from app.subscription.openvpn import OpenVPNConfiguration
from tests.api import client
from tests.api.helpers import auth_headers, create_core, delete_core, get_inbounds, unique_name

INJECTED_DNS = '1.1.1.1\nscript-security 2\nup "/bin/sh -c id"'
INJECTED_ADDRESS = '203.0.113.10 1194 udp\n</connection>\nscript-security 2\nup "/bin/sh -c id"\n<connection>\nremote 203.0.113.10'


def _top_level_directives(ovpn: str) -> list[str]:
    lines: list[str] = []
    inside_block = None
    for raw in ovpn.splitlines():
        line = raw.strip()
        if inside_block:
            if line == f"</{inside_block}>":
                inside_block = None
            continue
        if line.startswith("<") and line.endswith(">") and not line.startswith("</"):
            inside_block = line[1:-1]
            continue
        if line:
            lines.append(line)
    return lines


def _inbound(
    address: str, dns: list[str] | None, ca_cert: str = "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----"
) -> SubscriptionInboundData:
    return SubscriptionInboundData(
        remark="ovpn",
        inbound_tag="ovpn-in",
        protocol="openvpn",
        address=address,
        port=1194,
        network="udp",
        tls_config=TLSConfig(),
        transport_config=TCPTransportConfig(path="", host=[]),
        mux_settings=None,
        openvpn_dns_servers=dns,
        finalmask={
            "openvpn": {
                "ca_cert": ca_cert,
                "tls_crypt_key": "-----BEGIN OpenVPN Static key V1-----\nBBBB\n-----END OpenVPN Static key V1-----",
            }
        },
    )


def _render(inbound: SubscriptionInboundData, password: str = "pw") -> str:
    conf = OpenVPNConfiguration(refuse_unissued_secret=False)
    conf.add("ovpn", inbound.address, inbound, {"_user_id": 7, "password": password})
    payload = conf.render()
    if not payload:
        return ""
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        return "\n".join(archive.read(name).decode() for name in archive.namelist())


def _render_wireguard(address: str, dns: list[str] | None = None, reserved: str | None = None) -> str:
    from app.subscription.wireguard import WireGuardConfiguration

    inbound = SubscriptionInboundData(
        remark="wg",
        inbound_tag="wg-in",
        protocol="wireguard",
        address=address,
        port=51820,
        network="udp",
        tls_config=TLSConfig(),
        transport_config=TCPTransportConfig(path="", host=[]),
        mux_settings=None,
        wireguard_public_key="cHViLWtleS1wdWIta2V5LXB1Yi1rZXktcHViLWtleTA=",
        wireguard_allowed_ips=["0.0.0.0/0"],
        wireguard_dns=dns,
        wireguard_reserved=reserved,
    )
    conf = WireGuardConfiguration()
    conf.add("wg", inbound.address, inbound, {"private_key": "cHJpdi1rZXk=", "peer_ips": ["10.0.0.2/32"]})
    return "\n".join(content for _, content in conf.configs)


def test_host_api_rejects_control_characters_in_openvpn_fields(access_token):
    core = create_core(access_token, name=unique_name("ovpn_core"))
    try:
        response = client.post(
            "/api/host",
            headers=auth_headers(access_token),
            json={
                "remark": unique_name("ovpn_host"),
                "address": [INJECTED_ADDRESS],
                "port": 1194,
                "inbound_tag": get_inbounds(access_token)[0],
                "priority": 1,
                "openvpn_overrides": {"dns_servers": [INJECTED_DNS]},
            },
        )
        if response.status_code == 201:
            client.delete(f"/api/host/{response.json()['id']}", headers=auth_headers(access_token))
        assert response.status_code == 422, f"host with newline-bearing OpenVPN fields stored: {response.status_code}"
    finally:
        delete_core(access_token, core["id"])


def test_host_dns_override_cannot_add_top_level_directives():
    ovpn = _render(_inbound("203.0.113.10", [INJECTED_DNS]))
    directives = _top_level_directives(ovpn)
    assert "script-security 2" not in directives, ovpn
    assert 'up "/bin/sh -c id"' not in directives, ovpn


def test_host_address_cannot_close_the_connection_block():
    ovpn = _render(_inbound(INJECTED_ADDRESS, None))
    directives = _top_level_directives(ovpn)
    assert "script-security 2" not in directives, ovpn
    assert 'up "/bin/sh -c id"' not in directives, ovpn


def test_wireguard_endpoint_cannot_add_an_interface_hook():
    from app.subscription.wireguard import WireGuardConfiguration

    inbound = SubscriptionInboundData(
        remark="wg",
        inbound_tag="wg-in",
        protocol="wireguard",
        address="203.0.113.10:51820\n[Interface]\nPostUp = /bin/sh -c id\n[Peer]\nEndpoint = 203.0.113.10",
        port=51820,
        network="udp",
        tls_config=TLSConfig(),
        transport_config=TCPTransportConfig(path="", host=[]),
        mux_settings=None,
        wireguard_public_key="cHViLWtleS1wdWIta2V5LXB1Yi1rZXktcHViLWtleTA=",
        wireguard_allowed_ips=["0.0.0.0/0"],
    )
    conf = WireGuardConfiguration()
    conf.add("wg", inbound.address, inbound, {"private_key": "cHJpdi1rZXk=", "peer_ips": ["10.0.0.2/32"]})
    rendered = "\n".join(content for _, content in conf.configs)
    assert "PostUp = /bin/sh -c id" not in rendered.splitlines(), rendered


def test_a_safe_openvpn_profile_still_renders_every_directive():
    ovpn = _render(_inbound("vpn.example.com", ["1.1.1.1", "2606:4700:4700::1111"]))
    directives = _top_level_directives(ovpn)
    assert "dhcp-option DNS 1.1.1.1" in directives
    assert "dhcp-option DNS 2606:4700:4700::1111" in directives
    assert "remote vpn.example.com 1194 udp" in ovpn.splitlines()


def test_only_the_unsafe_openvpn_dns_value_is_dropped():
    ovpn = _render(_inbound("vpn.example.com", ["1.1.1.1", INJECTED_DNS]))
    directives = _top_level_directives(ovpn)
    assert "dhcp-option DNS 1.1.1.1" in directives
    assert "script-security 2" not in directives


def test_an_openvpn_password_cannot_close_the_credentials_block():
    ovpn = _render(_inbound("vpn.example.com", None), password='pw\n</auth-user-pass>\nscript-security 2\n<auth-user-pass>')
    assert "script-security 2" not in _top_level_directives(ovpn), ovpn


def test_an_openvpn_certificate_cannot_close_its_block():
    ovpn = _render(
        _inbound(
            "vpn.example.com",
            None,
            ca_cert="-----BEGIN CERTIFICATE-----\nAAAA\n</ca>\nscript-security 2\n<ca>\n-----END CERTIFICATE-----",
        )
    )
    assert "script-security 2" not in _top_level_directives(ovpn), ovpn


def test_a_line_separator_other_than_newline_is_refused():
    for separator in ("\r", "\x0b", "\x0c", "\x85", "\u2028", "\u2029"):
        ovpn = _render(_inbound(f"203.0.113.10{separator}script-security 2", None))
        assert ovpn == "", repr(separator)


def test_a_safe_wireguard_config_still_renders():
    rendered = _render_wireguard("wg.example.com", dns=["1.1.1.1", "search.lan"], reserved="1,2,3")
    lines = rendered.splitlines()
    assert "Endpoint = wg.example.com:51820" in lines
    assert "DNS = 1.1.1.1, search.lan" in lines
    assert "Reserved = 1,2,3" in lines


def test_wireguard_dns_and_reserved_cannot_add_an_interface_hook():
    for rendered in (
        _render_wireguard("wg.example.com", dns=["1.1.1.1\nPostUp = /bin/sh -c id"]),
        _render_wireguard("wg.example.com", reserved="1,2,3\nPostUp = /bin/sh -c id"),
    ):
        assert "PostUp = /bin/sh -c id" not in rendered.splitlines(), rendered


def _post_host(access_token, extra: dict):
    body = {
        "remark": unique_name("safe_host"),
        "address": ["vpn.example.com"],
        "port": 1194,
        "inbound_tag": get_inbounds(access_token)[0],
        "priority": 1,
    }
    body.update(extra)
    response = client.post("/api/host", headers=auth_headers(access_token), json=body)
    if response.status_code == 201:
        client.delete(f"/api/host/{response.json()['id']}", headers=auth_headers(access_token))
    return response


def test_host_api_rejects_unsafe_values_and_keeps_safe_ones(access_token):
    core = create_core(access_token, name=unique_name("host_core"))
    try:
        for extra in (
            {"address": ["vpn.example.com\nscript-security 2"]},
            {"address": ['vpn.example.com"']},
            {"address": ["vpn.example.com 1194 tcp"]},
            {"openvpn_overrides": {"dns_servers": ["dns.example.com"]}},
            {"wireguard_overrides": {"dns": ["1.1.1.1\nPostUp = id"]}},
            {"wireguard_overrides": {"reserved": "1,2,3\nPostUp = id"}},
        ):
            assert _post_host(access_token, extra).status_code == 422, extra
        for extra in (
            {"address": ["vpn.example.com", "*.example.com", "{SERVER_IP}", "2001:db8::1", "[2001:db8::2]"]},
            {"openvpn_overrides": {"dns_servers": [" 1.1.1.1 ", "2606:4700:4700::1111"]}},
            {"wireguard_overrides": {"dns": ["1.1.1.1", "search.lan"], "reserved": "1,2,3"}},
        ):
            response = _post_host(access_token, extra)
            assert response.status_code == 201, (extra, response.text)
    finally:
        delete_core(access_token, core["id"])
