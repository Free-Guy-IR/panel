import base64
import binascii

import pytest
from fastapi import status

from app.subscription.config_cache import clear_sub_config_cache
from tests.api import client
from tests.api.helpers import (
    auth_headers,
    create_core,
    create_group,
    create_hosts_for_inbounds,
    create_user,
    delete_core,
    delete_group,
    delete_user,
    get_inbound_details,
    unique_name,
)

CONFIG_FORMATS = ("links", "links_base64", "xray", "clash", "sing_box")
REAL_ADDRESS = "real-server.example.com"
PLACEHOLDER_ADDRESS = "renew-your-plan.example.com"
OWNER_DECISION = (
    "owner decision pending (2026-09-25): upstream PR #756 answers 403 to an inactive user, "
    "the alternative serves only status-scoped placeholder entries without credentials"
)


def _credentials(user):
    proxy = user.get("proxy_settings") or {}
    values = [
        (proxy.get("vless") or {}).get("id"),
        (proxy.get("vmess") or {}).get("id"),
        (proxy.get("trojan") or {}).get("password"),
    ]
    return [str(value) for value in values if value]


def _bodies(response):
    bodies = [response.text]
    try:
        bodies.append(base64.b64decode(response.content + b"===").decode(errors="ignore"))
    except (binascii.Error, ValueError):
        pass
    return "\n".join(bodies)


@pytest.fixture
def disabled_subscriber(access_token):
    clear_sub_config_cache()
    core = create_core(access_token)
    group = create_group(access_token, name=unique_name("inactive_group"))
    hosts = create_hosts_for_inbounds(access_token, address=[REAL_ADDRESS])
    inbound = next(item for item in get_inbound_details(access_token) if item["protocol"] == "vless")
    placeholder = client.post(
        "/api/host",
        headers=auth_headers(access_token),
        json={
            "remark": unique_name("placeholder"),
            "address": [PLACEHOLDER_ADDRESS],
            "port": 443,
            "inbound_tag": inbound["tag"],
            "priority": 999,
            "status": ["disabled", "expired", "limited"],
        },
    )
    assert placeholder.status_code == status.HTTP_201_CREATED, placeholder.text
    hosts.append(placeholder.json())
    user = create_user(access_token, group_ids=[group["id"]], payload={"username": unique_name("inactive")})
    disabled = client.put(
        f"/api/user/by-id/{user['id']}/disabled", headers=auth_headers(access_token), json={"disabled": True}
    )
    assert disabled.status_code == status.HTTP_200_OK
    yield user
    delete_user(access_token, user["username"])
    for host in hosts:
        client.delete(f"/api/host/{host['id']}", headers=auth_headers(access_token))
    delete_group(access_token, group["id"])
    delete_core(access_token, core["id"])


@pytest.mark.xfail(strict=True, reason=OWNER_DECISION)
def test_option_upstream_an_inactive_user_gets_403_for_every_config_route(disabled_subscriber):
    url = disabled_subscriber["subscription_url"]
    for config_format in CONFIG_FORMATS:
        assert client.get(f"{url}/{config_format}").status_code == status.HTTP_403_FORBIDDEN
    assert client.get(url, headers={"User-Agent": "v2rayNG/1.8.5"}).status_code == status.HTTP_403_FORBIDDEN
    raw = client.get(f"{url}/raw")
    assert raw.status_code == status.HTTP_200_OK
    assert raw.json()["body"]["links"] == []
    page = client.get(url, headers={"Accept": "text/html"})
    assert page.status_code == status.HTTP_200_OK
    assert 'class="link-input"' not in page.text


@pytest.mark.xfail(strict=True, reason=OWNER_DECISION)
def test_option_placeholders_an_inactive_user_gets_only_placeholder_entries(disabled_subscriber):
    url = disabled_subscriber["subscription_url"]
    credentials = _credentials(disabled_subscriber)
    assert credentials
    for config_format in CONFIG_FORMATS:
        response = client.get(f"{url}/{config_format}")
        assert response.status_code == status.HTTP_200_OK
        body = _bodies(response)
        assert PLACEHOLDER_ADDRESS in body
        assert REAL_ADDRESS not in body
        assert not any(secret in body for secret in credentials)
    raw = client.get(f"{url}/raw").json()["body"]["links"]
    assert raw
    assert all(REAL_ADDRESS not in link for link in raw)


def test_today_an_inactive_user_still_receives_real_hosts_and_credentials(disabled_subscriber):
    url = disabled_subscriber["subscription_url"]
    credentials = _credentials(disabled_subscriber)
    body = _bodies(client.get(f"{url}/links"))
    assert REAL_ADDRESS in body
    assert any(secret in body for secret in credentials)
