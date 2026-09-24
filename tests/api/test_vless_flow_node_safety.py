from copy import deepcopy

import pytest
from fastapi import status

from app.node.user import _serialize_user_for_node
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
from tests.api.sample_data import XRAY_CONFIG

XRAY_ACCEPTED_CLIENT_FLOWS = {"", "xtls-rprx-vision"}


def _create(access_token, flow):
    return client.post(
        "/api/user",
        headers=auth_headers(access_token),
        json={
            "username": unique_name("u2flow"),
            "proxy_settings": {"vless": {"flow": flow}},
            "data_limit": 1024 * 1024,
            "data_limit_reset_strategy": "no_reset",
            "status": "active",
        },
    )


@pytest.mark.parametrize("flow", ["xtls-rprx-vision-udp443", "not-a-flow"])
def test_vless_flow_pushed_to_node_is_one_xray_accepts(access_token, flow):
    response = _create(access_token, flow)
    if response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY:
        return
    assert response.status_code == status.HTTP_201_CREATED
    user = response.json()
    try:
        proto = _serialize_user_for_node(user["id"], user["proxy_settings"])
        assert proto.proxies.vless.flow in XRAY_ACCEPTED_CLIENT_FLOWS, (
            f"stored flow {user['proxy_settings']['vless'].get('flow')!r} reaches the node as "
            f"{proto.proxies.vless.flow!r}"
        )
    finally:
        delete_user(access_token, user["username"])


def test_vless_vision_flow_still_reaches_node(access_token):
    response = _create(access_token, "xtls-rprx-vision")
    assert response.status_code == status.HTTP_201_CREATED
    user = response.json()
    try:
        proto = _serialize_user_for_node(user["id"], user["proxy_settings"])
        assert proto.proxies.vless.flow == "xtls-rprx-vision"
    finally:
        delete_user(access_token, user["username"])


def test_subscription_uses_inbound_flow_for_vless_udp443(access_token):
    """Inbound flow should be used even when user proxy settings do not define one."""
    config = deepcopy(XRAY_CONFIG)
    inbound = next(item for item in config["inbounds"] if item["tag"] == "VLESS TCP REALITY")
    inbound["tag"] = unique_name("vless_flow_udp443")
    inbound["settings"]["flow"] = "xtls-rprx-vision-udp443"

    core = create_core(access_token, name=unique_name("flow_core"), config=config)
    group = create_group(access_token, name=unique_name("flow_group"), inbound_tags=[inbound["tag"]])
    host_response = client.post(
        "/api/host",
        headers=auth_headers(access_token),
        json={
            "remark": unique_name("flow_host"),
            "address": ["127.0.0.1"],
            "port": 443,
            "inbound_tag": inbound["tag"],
            "priority": 1,
            "sni": ["example.com"],
        },
    )
    assert host_response.status_code == status.HTTP_201_CREATED
    host = host_response.json()
    user = create_user(
        access_token,
        group_ids=[group["id"]],
        payload={"username": unique_name("test_flow_subscription")},
    )

    try:
        assert "flow" not in user["proxy_settings"]["vless"]

        response = client.get(f"{user['subscription_url']}/links")
        assert response.status_code == status.HTTP_200_OK
        assert "flow=xtls-rprx-vision-udp443" in response.text
    finally:
        delete_user(access_token, user["username"])
        client.delete(f"/api/host/{host['id']}", headers=auth_headers(access_token))
        delete_group(access_token, group["id"])
        delete_core(access_token, core["id"])
