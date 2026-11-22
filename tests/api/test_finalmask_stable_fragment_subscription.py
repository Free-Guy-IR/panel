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

LIST_ONLY_SETTINGS = {"packets": "tlshello", "lengths": ["100-200", "200-300"], "delays": ["10-20", "20-30"]}
STABLE_SETTINGS = {**LIST_ONLY_SETTINGS, "length": "100-200", "delay": "10-20"}
BLANK_FIRST_CASES = [
    pytest.param(
        {"packets": "tlshello", "lengths": ["", "100-200"], "delays": ["10-20"]},
        {"packets": "tlshello", "lengths": ["100-200"], "delays": ["10-20"], "length": "100-200", "delay": "10-20"},
        id="empty-first-length",
    ),
    pytest.param(
        {"packets": "tlshello", "lengths": ["100-200"], "delays": ["  ", "10-20"]},
        {"packets": "tlshello", "lengths": ["100-200"], "delays": ["10-20"], "length": "100-200", "delay": "10-20"},
        id="whitespace-first-delay",
    ),
]
WHERE = [pytest.param("host", id="host-mask"), pytest.param("inbound", id="inbound-mask")]


def _fragment_mask(settings: dict) -> dict:
    return {"tcp": [{"type": "fragment", "settings": settings}]}


def _served_fragment_settings(access_token: str, where: str, settings: dict, port: int) -> tuple[dict, dict]:
    headers = auth_headers(access_token)
    tag = unique_name("fm_stable_in")
    stream = {"network": "tcp"}
    if where == "inbound":
        stream["finalmask"] = _fragment_mask(settings)
    inbound = {
        "tag": tag,
        "protocol": "vless",
        "port": port,
        "settings": {"clients": [], "decryption": "none"},
        "streamSettings": stream,
    }
    core = create_core(
        access_token, config={"inbounds": [inbound], "outbounds": [{"tag": "direct", "protocol": "freedom"}]}
    )
    group = create_group(access_token, inbound_tags=[tag])
    user = create_user(access_token, group_ids=[group["id"]])
    remark = unique_name("fm_stable_host")
    body = {"remark": remark, "address": ["198.51.100.60"], "inbound_tag": tag, "priority": 1}
    if where == "host":
        body["final_mask_settings"] = _fragment_mask(settings)
    created = client.post("/api/host", headers=headers, json=body)
    assert created.status_code == 201, created.text
    try:
        xray = client.get(f"{user['subscription_url']}/xray")
        assert xray.status_code == 200, xray.text
        proxies = [
            outbound
            for config in xray.json()
            if config.get("remarks") == remark
            for outbound in config.get("outbounds", [])
            if outbound.get("tag") == "proxy"
        ]
        assert len(proxies) == 1, xray.text
        links = client.get(f"{user['subscription_url']}/links")
        assert links.status_code == 200, links.text
        matching = [line for line in links.text.splitlines() if unquote(urlparse(line).fragment) == remark]
        assert len(matching) == 1, links.text
        link_mask = json.loads(parse_qs(urlparse(matching[0]).query)["fm"][0])
        return proxies[0]["streamSettings"]["finalmask"]["tcp"][0]["settings"], link_mask["tcp"][0]["settings"]
    finally:
        client.delete(f"/api/host/{created.json()['id']}", headers=headers)
        delete_user(access_token, user["username"])
        delete_group(access_token, group["id"])
        delete_core(access_token, core["id"])


@pytest.mark.parametrize("where", WHERE)
def test_a_list_only_fragment_is_served_with_the_scalars_stable_xray_reads(access_token, where):
    xray_settings, link_settings = _served_fragment_settings(access_token, where, LIST_ONLY_SETTINGS, 18481)

    assert xray_settings == STABLE_SETTINGS
    assert link_settings == STABLE_SETTINGS


@pytest.mark.parametrize("where", WHERE)
@pytest.mark.parametrize(("stored", "served"), BLANK_FIRST_CASES)
def test_a_blank_first_fragment_entry_does_not_hide_the_scalar(access_token, where, stored, served):
    xray_settings, link_settings = _served_fragment_settings(access_token, where, stored, 18482)

    assert xray_settings == served
    assert link_settings == served
