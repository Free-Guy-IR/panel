import base64
import json
from urllib.parse import parse_qs, urlsplit

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

FRAGMENT_CASES = [
    pytest.param({"delay": 0, "delays": [5]}, id="delay-zero-with-delays"),
    pytest.param({"delay": 0}, id="delay-zero-only"),
]


def _core_config(tag: str) -> dict:
    return {
        "inbounds": [
            {
                "tag": tag,
                "protocol": "vless",
                "port": 18561,
                "settings": {"clients": [], "decryption": "none"},
                "streamSettings": {"network": "tcp"},
            }
        ],
        "outbounds": [{"tag": "direct", "protocol": "freedom"}],
    }


@pytest.fixture
def fragment_host(access_token, request):
    headers = auth_headers(access_token)
    tag = unique_name("fd0_in")
    remark = unique_name("fd0")
    core = create_core(access_token, config=_core_config(tag))
    group = create_group(access_token, inbound_tags=[tag])
    user = create_user(access_token, group_ids=[group["id"]])
    host_id = None
    settings = {"packets": "tlshello", "lengths": ["100-200"], **request.param}
    try:
        response = client.post(
            "/api/host",
            headers=headers,
            json={
                "remark": remark,
                "address": ["198.51.100.41"],
                "inbound_tag": tag,
                "priority": 1,
                "final_mask_settings": {"tcp": [{"type": "fragment", "settings": settings}]},
            },
        )
        assert response.status_code == 201, response.text
        host_id = response.json()["id"]
        yield user, remark
    finally:
        if host_id is not None:
            client.delete(f"/api/host/{host_id}", headers=headers)
        delete_user(access_token, user["username"])
        delete_group(access_token, group["id"])
        delete_core(access_token, core["id"])


def _xray_fragment(user: dict, remark: str) -> dict:
    response = client.get(f"{user['subscription_url']}/xray")
    assert response.status_code == 200, response.text
    configs = {config.get("remarks"): config for config in response.json()}
    finalmasks = [
        outbound["streamSettings"]["finalmask"]
        for outbound in configs[remark]["outbounds"]
        if isinstance(outbound.get("streamSettings"), dict) and "finalmask" in outbound["streamSettings"]
    ]
    assert len(finalmasks) == 1
    return finalmasks[0]["tcp"][0]["settings"]


def _link_fragment(user: dict, remark: str, client_type: str) -> dict:
    response = client.get(f"{user['subscription_url']}/{client_type}")
    assert response.status_code == 200, response.text
    body = base64.b64decode(response.text).decode() if client_type == "links_base64" else response.text
    links = [line for line in body.splitlines() if line.startswith("vless://") and remark in line]
    assert len(links) == 1
    fm = parse_qs(urlsplit(links[0]).query)["fm"][0]
    return json.loads(fm)["tcp"][0]["settings"]


@pytest.mark.parametrize("fragment_host", FRAGMENT_CASES, indirect=True)
@pytest.mark.parametrize("client_type", ["links", "links_base64"])
def test_link_and_xray_json_carry_the_same_fragment_delay(fragment_host, client_type):
    user, remark = fragment_host
    xray = _xray_fragment(user, remark)
    link = _link_fragment(user, remark, client_type)

    assert link.get("delay") == xray.get("delay")
    assert link.get("delays") == xray.get("delays")
    assert link.get("length") == xray.get("length")
    assert link.get("lengths") == xray.get("lengths")
