from datetime import UTC, datetime, timedelta
from urllib.parse import unquote

import pytest
from fastapi import status

import app.settings as settings_module
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

SERVED_RAW_TO_SOME_USERS = [
    "{USERNAME:d}",
    "{USERNAME:,.2f}",
    "{DAYS_LEFT:,}",
    "{DAYS_LEFT:03d}",
    "{USAGE_PERCENTAGE:.1f}",
    "{DAYS_LEFT:.2}",
    "{DAYS_LEFT:s}",
    "{USAGE_PERCENTAGE:s}",
]
RENDERED_FOR_EVERY_USER = [
    "{DAYS_LEFT}",
    "{DAYS_LEFT:>4}",
    "{USAGE_PERCENTAGE:.3}",
    "{USERNAME:>12}",
    "{DAYS_LEFT!s:>3}",
]


@pytest.fixture
def promo_users(access_token):
    headers = auth_headers(access_token)
    core = create_core(access_token, name=unique_name("promo_core"))
    inbound = client.get("/api/inbounds", headers=headers).json()[0]
    host = client.post(
        "/api/host",
        headers=headers,
        json={"remark": "promo-{PROMO}", "address": ["127.0.0.1"], "port": 443, "inbound_tag": inbound, "priority": 1},
    )
    assert host.status_code == status.HTTP_201_CREATED, host.text
    group = create_group(access_token, name=unique_name("promo_group"))
    expire = (datetime.now(UTC) + timedelta(days=3)).isoformat()
    users = [
        create_user(access_token, group_ids=[group["id"]], payload={"username": unique_name("unl"), "data_limit": 0}),
        create_user(
            access_token,
            group_ids=[group["id"]],
            payload={"username": unique_name("lim"), "data_limit": 1024**3, "expire": expire},
        ),
    ]
    yield users
    for user in users:
        delete_user(access_token, user["username"])
    client.delete(f"/api/host/{host.json()['id']}", headers=headers)
    delete_group(access_token, group["id"])
    delete_core(access_token, core["id"])


def _save(headers: dict, before: dict, value: str):
    body = {**before["subscription"], "custom_variables": [{"key": "PROMO", "value": value}]}
    return client.put("/api/settings", headers=headers, json={"subscription": body})


def _served_remarks(headers: dict, before: dict, users: list[dict], value: str) -> list[str]:
    stored = settings_module.get_settings.return_value
    kept = stored.subscription
    stored.subscription = {**kept, "custom_variables": [{"key": "PROMO", "value": value}]}
    refresh = {"general": {"default_method": before["general"]["default_method"]}}
    try:
        assert client.put("/api/settings", headers=headers, json=refresh).status_code == status.HTTP_200_OK
        remarks = []
        for user in users:
            links = client.get(f"{user['subscription_url']}/links")
            assert links.status_code == status.HTTP_200_OK, links.text
            remarks += [unquote(line.rsplit("#", 1)[1]) for line in links.text.splitlines() if "#promo-" in line]
        return remarks
    finally:
        stored.subscription = kept
        client.put("/api/settings", headers=headers, json=refresh)


@pytest.mark.parametrize("value", SERVED_RAW_TO_SOME_USERS + RENDERED_FOR_EVERY_USER)
def test_a_custom_variable_that_saves_renders_for_every_user(access_token, promo_users, value):
    headers = auth_headers(access_token)
    before = client.get("/api/settings", headers=headers).json()
    saved = _save(headers, before, value)
    client.put("/api/settings", headers=headers, json={"subscription": before["subscription"]})
    if saved.status_code != status.HTTP_200_OK:
        assert saved.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, saved.text
        return
    remarks = _served_remarks(headers, before, promo_users, value)
    assert len(remarks) == len(promo_users)
    assert [remark for remark in remarks if "{" in remark] == []


@pytest.mark.parametrize("value", RENDERED_FOR_EVERY_USER)
def test_a_format_spec_every_value_of_its_variable_fits_still_saves(access_token, value):
    headers = auth_headers(access_token)
    before = client.get("/api/settings", headers=headers).json()
    saved = _save(headers, before, value)
    client.put("/api/settings", headers=headers, json={"subscription": before["subscription"]})
    assert saved.status_code == status.HTTP_200_OK, saved.text
