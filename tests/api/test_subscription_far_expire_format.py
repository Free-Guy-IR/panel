from urllib.parse import unquote

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

FAR_EXPIRE_CASES = [
    pytest.param("9999-12-30T00:00:00Z", "{USERNAME}", id="expire-beyond-jalali-range"),
    pytest.param("9999-12-30T00:00:00Z", "{JALALI_EXPIRE_DATE}", id="jalali-expire-date-beyond-range"),
]


def _core_config(tag: str) -> dict:
    return {
        "inbounds": [
            {
                "tag": tag,
                "protocol": "vless",
                "port": 18571,
                "settings": {"clients": [], "decryption": "none"},
                "streamSettings": {"network": "tcp"},
            }
        ],
        "outbounds": [{"tag": "direct", "protocol": "freedom"}],
    }


@pytest.mark.parametrize("expire,remark_format", FAR_EXPIRE_CASES)
@pytest.mark.parametrize("client_type", ["", "/links", "/xray", "/info"])
def test_a_far_expire_never_turns_the_subscription_into_a_500(access_token, expire, remark_format, client_type):
    headers = auth_headers(access_token)
    tag = unique_name("fex_in")
    core = create_core(access_token, config=_core_config(tag))
    group = create_group(access_token, inbound_tags=[tag])
    user = create_user(access_token, group_ids=[group["id"]], payload={"expire": expire})
    host_id = None
    try:
        response = client.post(
            "/api/host",
            headers=headers,
            json={
                "remark": f"fex-{remark_format}",
                "address": ["198.51.100.51"],
                "inbound_tag": tag,
                "priority": 1,
            },
        )
        assert response.status_code == 201, response.text
        host_id = response.json()["id"]

        response = client.get(f"{user['subscription_url']}{client_type}")
        assert response.status_code == 200, response.text
    finally:
        if host_id is not None:
            client.delete(f"/api/host/{host_id}", headers=headers)
        delete_user(access_token, user["username"])
        delete_group(access_token, group["id"])
        delete_core(access_token, core["id"])


@pytest.mark.parametrize(
    "expire,rendered",
    [("9999-12-30T00:00:00Z", "fexj-∞-end"), (None, "fexj-∞-end"), ("0026-01-01T00:00:00Z", "fexj---end")],
)
def test_an_expire_past_the_jalali_range_renders_like_no_expire(access_token, expire, rendered):
    headers = auth_headers(access_token)
    tag = unique_name("fexj_in")
    core = create_core(access_token, config=_core_config(tag))
    group = create_group(access_token, inbound_tags=[tag])
    user = create_user(access_token, group_ids=[group["id"]], payload={"expire": expire})
    host_id = None
    try:
        response = client.post(
            "/api/host",
            headers=headers,
            json={
                "remark": "fexj-{JALALI_EXPIRE_DATE}-end",
                "address": ["198.51.100.52"],
                "inbound_tag": tag,
                "priority": 1,
            },
        )
        assert response.status_code == 201, response.text
        host_id = response.json()["id"]

        response = client.get(f"{user['subscription_url']}/links")
        assert response.status_code == 200, response.text
        assert rendered in unquote(response.text), response.text
    finally:
        if host_id is not None:
            client.delete(f"/api/host/{host_id}", headers=headers)
        delete_user(access_token, user["username"])
        delete_group(access_token, group["id"])
        delete_core(access_token, core["id"])
