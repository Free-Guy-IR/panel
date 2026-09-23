import pytest

from app.db.models import User
from app.models.settings import ConfigFormat, HWIDSettings
from tests.api import TestSession, client
from tests.api.helpers import (
    auth_headers,
    create_core,
    create_group,
    create_hosts_for_inbounds,
    create_user,
    delete_core,
    delete_group,
    delete_user,
    unique_name,
)


@pytest.fixture
def hwid_locked_user(access_token, monkeypatch):
    async def locked_policy():
        return HWIDSettings(enabled=True, forced=True, require_hwid_for_manual_sub=True, fallback_limit=1)

    monkeypatch.setattr("app.operation.subscription.hwid_settings", locked_policy)
    core = create_core(access_token, name=unique_name("hwid_core"))
    hosts = create_hosts_for_inbounds(access_token)
    group = create_group(access_token, name=unique_name("hwid_group"))
    user = create_user(access_token, group_ids=[group["id"]], payload={"username": unique_name("hwid_user")})
    yield user
    delete_user(access_token, user["username"])
    for host in hosts:
        client.delete(f"/api/host/{host['id']}", headers=auth_headers(access_token))
    delete_group(access_token, group["id"])
    delete_core(access_token, core["id"])


def test_raw_endpoint_honours_the_device_lock_the_page_and_manual_routes_enforce(hwid_locked_user):
    uuid = hwid_locked_user["proxy_settings"]["vless"]["id"]
    manual = client.get(f"{hwid_locked_user['subscription_url']}/links")
    assert manual.status_code == 403
    page = client.get(hwid_locked_user["subscription_url"], headers={"Accept": "text/html"})
    assert page.status_code == 200
    assert uuid not in page.text

    raw = client.get(f"{hwid_locked_user['subscription_url']}/raw")
    info = client.get(f"{hwid_locked_user['subscription_url']}/info")
    assert raw.status_code == 200
    assert info.status_code == 200
    leaks = {
        "raw.body.links": any(uuid in link for link in raw.json()["body"]["links"]),
        "raw.body.user.proxy_settings": uuid in str(raw.json()["body"]["user"].get("proxy_settings")),
        "info.proxy_settings": uuid in str(info.json().get("proxy_settings")),
    }
    assert not any(leaks.values()), f"device-locked secrets served without X-HWID: {leaks}"


FORK_SECRETS = {
    "mtproto": {"secret": "ef" * 16},
    "l2tp": {"password": "L2tpSecret-7788"},
    "openvpn": {"password": "OvpnSecret-99887766"},
}


def _secret_strings(proxy_settings: dict) -> set[str]:
    found: set[str] = set()
    for key, value in proxy_settings.items():
        if isinstance(value, dict):
            found |= _secret_strings(value)
        elif isinstance(value, str) and len(value) >= 12 and key not in ("method", "flow", "public_key"):
            found.add(value)
    return found


@pytest.mark.asyncio
async def test_no_subscription_route_hands_a_device_locked_secret_out_without_x_hwid(access_token, hwid_locked_user):
    async with TestSession() as db:
        db_user = await db.get(User, hwid_locked_user["id"])
        db_user.proxy_settings = {**db_user.proxy_settings, **FORK_SECRETS}
        await db.commit()
    stored = client.get(f"/api/user/{hwid_locked_user['username']}", headers=auth_headers(access_token)).json()
    secrets = _secret_strings(stored["proxy_settings"])
    assert {"ef" * 16, "L2tpSecret-7788", "OvpnSecret-99887766", stored["proxy_settings"]["vless"]["id"]} <= secrets

    base = hwid_locked_user["subscription_url"]
    responses = {
        "page": client.get(base, headers={"Accept": "text/html"}),
        "head": client.head(base, headers={"User-Agent": "v2rayNG/1.8.5"}),
        "info": client.get(f"{base}/info"),
        "raw": client.get(f"{base}/raw"),
        "apps": client.get(f"{base}/apps"),
        "usage": client.get(f"{base}/usage"),
        "ping": client.get(f"{base}/ping"),
        "ua": client.get(base, headers={"User-Agent": "v2rayNG/1.8.5"}),
    }
    for fmt in ConfigFormat:
        if fmt is not ConfigFormat.block:
            responses[fmt.value] = client.get(f"{base}/{fmt.value}")

    leaks = {}
    for name, response in responses.items():
        exposed = " ".join([response.text, *response.headers.values()])
        hits = sorted(secret for secret in secrets if secret in exposed)
        if hits:
            leaks[name] = (response.status_code, hits)
    assert not leaks, f"device-locked secrets served without X-HWID: {leaks}"
