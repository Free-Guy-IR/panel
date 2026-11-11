import json
from urllib.parse import parse_qs, unquote, urlparse

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

TCP_FILTER = {"type": "filter", "settings": {"allowedSourceIPs": ["10.0.0.0/8"], "blockedSourceIPs": ["10.6.6.6"]}}
UDP_FILTER = {"type": "filter", "settings": {"blockedSourceIPs": ["192.0.2.0/24"]}}
FRAGMENT = {"type": "fragment", "settings": {"packets": "tlshello", "lengths": ["100-200"], "delays": ["10-20"]}}
FILTER_ADDRESSES = ("10.0.0.0/8", "10.6.6.6", "192.0.2.0/24")


@pytest.fixture
def filtered_inbound_user(access_token):
    headers = auth_headers(access_token)
    tag = unique_name("fm_filter_in")
    inbound = {
        "tag": tag,
        "protocol": "vless",
        "port": 18471,
        "settings": {"clients": [], "decryption": "none"},
        "streamSettings": {"network": "tcp", "finalmask": {"tcp": [TCP_FILTER, FRAGMENT], "udp": [UDP_FILTER]}},
    }
    core = create_core(
        access_token, config={"inbounds": [inbound], "outbounds": [{"tag": "direct", "protocol": "freedom"}]}
    )
    group = create_group(access_token, inbound_tags=[tag])
    user = create_user(access_token, group_ids=[group["id"]])
    remark = unique_name("fm_filter_host")
    response = client.post(
        "/api/host",
        headers=headers,
        json={"remark": remark, "address": ["198.51.100.50"], "inbound_tag": tag, "priority": 1},
    )
    assert response.status_code == 201, response.text
    host_id = response.json()["id"]
    try:
        yield user, remark
    finally:
        client.delete(f"/api/host/{host_id}", headers=headers)
        delete_user(access_token, user["username"])
        delete_group(access_token, group["id"])
        delete_core(access_token, core["id"])


def _layer_types(finalmask: dict) -> list[str]:
    return [layer.get("type") for layers in finalmask.values() for layer in layers]


def test_the_xray_subscription_never_carries_the_inbound_filter_layers(filtered_inbound_user):
    user, remark = filtered_inbound_user
    response = client.get(f"{user['subscription_url']}/xray")
    assert response.status_code == 200, response.text

    proxies = [
        outbound
        for config in response.json()
        if config.get("remarks") == remark
        for outbound in config.get("outbounds", [])
        if outbound.get("tag") == "proxy"
    ]
    assert len(proxies) == 1
    finalmask = proxies[0]["streamSettings"]["finalmask"]
    assert _layer_types(finalmask) == ["fragment"]
    assert "udp" not in finalmask
    assert not any(address in response.text for address in FILTER_ADDRESSES)


def test_the_finalmask_link_parameter_never_carries_the_inbound_filter_layers(filtered_inbound_user):
    user, remark = filtered_inbound_user
    response = client.get(f"{user['subscription_url']}/links")
    assert response.status_code == 200, response.text

    links = [line for line in response.text.splitlines() if unquote(urlparse(line).fragment) == remark]
    assert len(links) == 1
    finalmask = json.loads(parse_qs(urlparse(links[0]).query)["fm"][0])
    assert _layer_types(finalmask) == ["fragment"]
    assert "udp" not in finalmask
    assert not any(address in response.text for address in FILTER_ADDRESSES)
