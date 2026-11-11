import base64
import json

import pytest

from tests.api import client
from tests.api.helpers import (
    auth_headers,
    create_core,
    create_group,
    create_user,
    delete_core,
    delete_group,
    delete_user,
    unique_name,
)

XDRIVE_SETTINGS = {"remoteFolder": "pg-test", "service": "gdrive", "secrets": ["test-secret"]}
INBOUND_XDRIVE_XMUX = {"maxConcurrency": "4-8"}
HOST_XMUX = {"maxConnections": "2-3", "hKeepAlivePeriod": 30}


def _masque_inbound(tag: str) -> dict:
    return {
        "tag": tag,
        "protocol": "masque",
        "port": 18451,
        "settings": {"users": [], "remoteDNS": ["9.9.9.9"]},
        "streamSettings": {
            "method": "h3",
            "masqueSettings": {"path": "/masque", "host": "masque.example", "headers": {"X-Inbound": "1"}},
        },
    }


def _xdrive_inbound(tag: str) -> dict:
    return {
        "tag": tag,
        "protocol": "vless",
        "port": 18452,
        "settings": {"clients": [], "decryption": "none"},
        "streamSettings": {"network": "xdrive", "xdriveSettings": {**XDRIVE_SETTINGS, "xmux": INBOUND_XDRIVE_XMUX}},
    }


def _vless_inbound(tag: str) -> dict:
    return {
        "tag": tag,
        "protocol": "vless",
        "port": 18453,
        "settings": {"clients": [], "decryption": "none"},
        "streamSettings": {"network": "tcp"},
    }


def _served_user(access_token, inbounds, hosts):
    headers = auth_headers(access_token)
    config = {"inbounds": inbounds, "outbounds": [{"tag": "direct", "protocol": "freedom"}]}
    core = create_core(access_token, config=config)
    group = create_group(access_token, inbound_tags=[inbound["tag"] for inbound in inbounds])
    user = create_user(access_token, group_ids=[group["id"]])
    host_ids = []
    for host in hosts:
        payload = {"address": ["198.51.100.40"], "priority": 1, "inbound_tag": inbounds[0]["tag"], **host}
        response = client.post("/api/host", headers=headers, json=payload)
        assert response.status_code == 201, response.text
        host_ids.append(response.json()["id"])

    def cleanup():
        for host_id in host_ids:
            client.delete(f"/api/host/{host_id}", headers=headers)
        delete_user(access_token, user["username"])
        delete_group(access_token, group["id"])
        delete_core(access_token, core["id"])

    return user, cleanup


@pytest.fixture
def masque_user(access_token):
    remarks = {"host": unique_name("masque_host"), "inbound": unique_name("masque_inbound"), "vless": unique_name("vl")}
    vless = _vless_inbound(unique_name("vless_in"))
    hosts = [
        {
            "remark": remarks["host"],
            "transport_settings": {"masque_settings": {"remote_dns": [" 1.1.1.1 ", ""], "xmux": HOST_XMUX}},
        },
        {"remark": remarks["inbound"]},
        {"remark": remarks["vless"], "inbound_tag": vless["tag"]},
    ]
    user, cleanup = _served_user(access_token, [_masque_inbound(unique_name("masque_in")), vless], hosts)
    try:
        yield user, remarks
    finally:
        cleanup()


@pytest.fixture
def xdrive_user(access_token):
    remarks = {"host": unique_name("xdrive_host"), "inbound": unique_name("xdrive_inbound")}
    hosts = [
        {"remark": remarks["host"], "transport_settings": {"xdrive_settings": {"xmux": HOST_XMUX}}},
        {"remark": remarks["inbound"]},
    ]
    user, cleanup = _served_user(access_token, [_xdrive_inbound(unique_name("xdrive_in"))], hosts)
    try:
        yield user, remarks
    finally:
        cleanup()


def _proxies_by_remark(user) -> dict[str, dict]:
    response = client.get(f"{user['subscription_url']}/xray")
    assert response.status_code == 200, response.text
    proxies = {}
    for config in response.json():
        for outbound in config.get("outbounds", []):
            if outbound.get("tag") == "proxy":
                proxies[config["remarks"]] = outbound
    return proxies


def test_a_masque_host_is_served_with_its_password_dns_and_xmux(access_token, masque_user):
    user, remarks = masque_user
    stored = client.get(f"/api/user/{user['username']}", headers=auth_headers(access_token))
    password = stored.json()["proxy_settings"]["masque"]["password"]

    outbound = _proxies_by_remark(user)[remarks["host"]]

    assert outbound["protocol"] == "masque"
    assert outbound["settings"]["address"] == "198.51.100.40"
    assert outbound["settings"]["port"] == 18451
    assert outbound["settings"]["remoteDNS"] == ["1.1.1.1"]
    assert outbound["streamSettings"]["method"] == "masque"
    masque = outbound["streamSettings"]["masqueSettings"]
    assert masque["pass"] == password
    assert masque["host"] == "masque.example"
    assert masque["path"] == "/masque"
    assert masque["headers"] == {"X-Inbound": "1"}
    assert masque["xmux"] == HOST_XMUX
    assert "mux" not in outbound


def test_a_masque_host_without_dns_falls_back_to_the_inbound_dns(masque_user):
    user, remarks = masque_user

    outbound = _proxies_by_remark(user)[remarks["inbound"]]

    assert outbound["settings"]["remoteDNS"] == ["9.9.9.9"]
    assert "xmux" not in outbound["streamSettings"]["masqueSettings"]


def test_an_xdrive_host_xmux_overrides_the_inbound_xmux_and_turns_mux_on(xdrive_user):
    user, remarks = xdrive_user
    proxies = _proxies_by_remark(user)

    host_outbound = proxies[remarks["host"]]
    inbound_outbound = proxies[remarks["inbound"]]

    assert host_outbound["streamSettings"]["xdriveSettings"]["xmux"] == HOST_XMUX
    assert inbound_outbound["streamSettings"]["xdriveSettings"]["xmux"] == INBOUND_XDRIVE_XMUX
    assert host_outbound["mux"] == {"enabled": True}
    assert inbound_outbound["mux"] == {"enabled": True}


@pytest.mark.parametrize("client_type", ["links", "links_base64", "sing_box", "clash_meta"])
def test_formats_without_masque_skip_only_the_masque_hosts(masque_user, client_type):
    user, remarks = masque_user
    response = client.get(f"{user['subscription_url']}/{client_type}")
    assert response.status_code == 200, response.text

    body = response.text
    if client_type == "links_base64":
        body = base64.b64decode(body).decode()
    assert remarks["host"] not in body
    assert remarks["inbound"] not in body
    assert remarks["vless"] in body
    if client_type == "sing_box":
        outbounds = json.loads(body)["outbounds"]
        tags = {outbound.get("tag") for outbound in outbounds}
        assert all(tag in tags for outbound in outbounds for tag in outbound.get("outbounds", []))
