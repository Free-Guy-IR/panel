import pytest

import app.settings as settings_module
from tests.api import client
from tests.api.helpers import (
    auth_headers,
    create_client_template,
    create_core,
    create_group,
    create_hosts_for_inbounds,
    create_user,
    delete_client_template,
    delete_core,
    delete_group,
    delete_user,
    unique_name,
)


@pytest.fixture
def user_with_hosts(access_token):
    core = create_core(access_token, name=unique_name("stale_core"))
    hosts = create_hosts_for_inbounds(access_token)
    group = create_group(access_token, name=unique_name("stale_group"))
    user = create_user(access_token, group_ids=[group["id"]], payload={"username": unique_name("stale_user")})
    yield user
    delete_user(access_token, user["username"])
    for host in hosts:
        client.delete(f"/api/host/{host['id']}", headers=auth_headers(access_token))
    delete_group(access_token, group["id"])
    delete_core(access_token, core["id"])


def test_links_after_revoke_carry_the_rotated_credentials(access_token, user_with_hosts):
    old_uuid = user_with_hosts["proxy_settings"]["vless"]["id"]
    before = client.get(f"{user_with_hosts['subscription_url']}/links")
    assert before.status_code == 200
    assert old_uuid in before.text

    revoked = client.post(f"/api/user/{user_with_hosts['username']}/revoke_sub", headers=auth_headers(access_token))
    assert revoked.status_code == 200
    new_uuid = revoked.json()["proxy_settings"]["vless"]["id"]
    assert new_uuid != old_uuid

    after = client.get(f"{revoked.json()['subscription_url']}/links")
    assert after.status_code == 200
    assert old_uuid not in after.text, "the new subscription token served the pre-revoke credentials"
    assert new_uuid in after.text


def test_a_new_user_never_receives_a_deleted_users_cached_config(access_token, user_with_hosts):
    group_ids = user_with_hosts["group_ids"]
    first = create_user(access_token, group_ids=group_ids, payload={"username": unique_name("reused_a")})
    first_uuid = first["proxy_settings"]["vless"]["id"]
    assert first_uuid in client.get(f"{first['subscription_url']}/links").text
    delete_user(access_token, first["username"])

    second = create_user(access_token, group_ids=group_ids, payload={"username": unique_name("reused_b")})
    try:
        served = client.get(f"{second['subscription_url']}/links").text
        assert first_uuid not in served, (
            f"user {second['username']} (id {second['id']}) was served the deleted user's config (id {first['id']})"
        )
    finally:
        delete_user(access_token, second["username"])


def _links(user: dict) -> str:
    response = client.get(f"{user['subscription_url']}/links")
    assert response.status_code == 200
    return response.text


def test_a_host_remark_edit_reaches_the_next_links_fetch(access_token):
    core = create_core(access_token, name=unique_name("remark_core"))
    hosts = create_hosts_for_inbounds(access_token)
    group = create_group(access_token, name=unique_name("remark_group"))
    user = create_user(access_token, group_ids=[group["id"]], payload={"username": unique_name("remark_user")})
    try:
        target = next(host for host in hosts if host["remark"] in _links(user))
        renamed = unique_name("renamed")
        payload = {key: value for key, value in target.items() if key != "id"}
        payload["remark"] = renamed
        modified = client.put(f"/api/host/{target['id']}", headers=auth_headers(access_token), json=payload)
        assert modified.status_code == 200, modified.text
        served = _links(user)
        assert renamed in served, "the host edit did not reach the subscription within the cache window"
        assert f"#{target['remark']}" not in served
    finally:
        delete_user(access_token, user["username"])
        for host in hosts:
            client.delete(f"/api/host/{host['id']}", headers=auth_headers(access_token))
        delete_group(access_token, group["id"])
        delete_core(access_token, core["id"])


def test_a_settings_change_reaches_the_next_links_fetch(access_token):
    stored = settings_module.get_settings.return_value
    default_method = client.get("/api/settings", headers=auth_headers(access_token)).json()["general"]["default_method"]
    core = create_core(access_token, name=unique_name("settings_core"))
    inbound = client.get("/api/inbounds", headers=auth_headers(access_token)).json()[0]
    host = client.post(
        "/api/host",
        headers=auth_headers(access_token),
        json={
            "remark": "probe-{CACHEPROBE}",
            "address": ["127.0.0.1"],
            "port": 443,
            "inbound_tag": inbound,
            "priority": 1,
        },
    )
    assert host.status_code == 201, host.text
    group = create_group(access_token, name=unique_name("settings_group"))
    user = create_user(access_token, group_ids=[group["id"]], payload={"username": unique_name("settings_user")})

    def set_probe(value: str) -> None:
        stored.subscription = {**stored.subscription, "custom_variables": [{"key": "CACHEPROBE", "value": value}]}
        response = client.put(
            "/api/settings",
            headers=auth_headers(access_token),
            json={"general": {"default_method": default_method}},
        )
        assert response.status_code == 200, response.text

    try:
        set_probe("before")
        assert "probe-before" in _links(user)
        set_probe("after")
        served = _links(user)
        assert "probe-after" in served, "the settings change did not reach the subscription within the cache window"
        assert "probe-before" not in served
    finally:
        delete_user(access_token, user["username"])
        client.delete(f"/api/host/{host.json()['id']}", headers=auth_headers(access_token))
        delete_group(access_token, group["id"])
        delete_core(access_token, core["id"])


def test_a_client_template_edit_reaches_the_next_clash_fetch(access_token, user_with_hosts):
    listed = client.get(
        "/api/client_templates",
        params={"template_type": "clash_subscription"},
        headers=auth_headers(access_token),
    )
    assert listed.status_code == 200
    previous_default = next((t for t in listed.json()["templates"] if t["is_default"]), None)
    body = "proxies: []\nproxy-groups: []\nrules: []\n"
    template = create_client_template(
        access_token,
        name=unique_name("cache_clash"),
        template_type="clash_subscription",
        content="mixed-port: 17891\n" + body,
        is_default=True,
    )
    try:
        first = client.get(f"{user_with_hosts['subscription_url']}/clash")
        assert first.status_code == 200, first.text
        assert "17891" in first.text
        modified = client.put(
            f"/api/client_template/{template['id']}",
            headers=auth_headers(access_token),
            json={"content": "mixed-port: 17892\n" + body},
        )
        assert modified.status_code == 200, modified.text
        second = client.get(f"{user_with_hosts['subscription_url']}/clash")
        assert second.status_code == 200
        assert "17892" in second.text, "the template edit did not reach the subscription within the cache window"
    finally:
        if previous_default is not None:
            client.put(
                f"/api/client_template/{previous_default['id']}",
                headers=auth_headers(access_token),
                json={"is_default": True},
            )
        delete_client_template(access_token, template["id"])
