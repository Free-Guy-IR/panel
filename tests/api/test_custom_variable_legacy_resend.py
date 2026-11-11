import asyncio
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote

import pytest
from fastapi import status
from sqlalchemy import select

from app.db.crud.settings import get_settings
from app.db.models import Admin
from tests.api import GetTestDB, client
from tests.api.helpers import (
    auth_headers,
    create_admin,
    create_core,
    create_group,
    delete_admin,
    delete_core,
    delete_group,
    unique_name,
)

LEGACY = ["{DAYS_LEFT:03d}", "{USERNAME:d}", "{USAGE_PERCENTAGE:.1f}"]
OLDER_LEGACY = ["{USERNAME:>300}", "{USERNAME[0]}", "{DAYS_LEFT:c}"]


async def _store_admin_variables(username: str, variables: list[dict]) -> None:
    async with GetTestDB() as db:
        admin = (await db.execute(select(Admin).where(Admin.username == username))).scalar_one()
        admin.custom_variables = variables
        await db.commit()


async def _stored_admin_variables(username: str) -> list[dict]:
    async with GetTestDB() as db:
        admin = (await db.execute(select(Admin).where(Admin.username == username))).scalar_one()
        return admin.custom_variables


async def _store_subscription_variables(variables: list[dict]) -> None:
    async with GetTestDB() as db:
        db_settings = await get_settings(db)
        db_settings.subscription = {**db_settings.subscription, "custom_variables": variables}
        await db.commit()


def _toggle_payload(admin: dict, variables: list[dict]) -> dict:
    return {
        "status": "disabled",
        "discord_webhook": admin.get("discord_webhook"),
        "sub_template": admin.get("sub_template"),
        "telegram_id": admin.get("telegram_id"),
        "support_url": admin.get("support_url"),
        "profile_title": admin.get("profile_title"),
        "sub_domain": admin.get("sub_domain"),
        "custom_variables": variables,
        "note": admin.get("note"),
    }


@pytest.mark.parametrize("value", LEGACY + OLDER_LEGACY)
@pytest.mark.parametrize("route", ["by_id", "by_username"])
def test_the_admin_toggle_resends_a_stored_legacy_value_and_saves(access_token, value, route):
    headers = auth_headers(access_token)
    admin = create_admin(access_token)
    variables = [{"key": "PROMO", "value": value}, {"key": "PLAIN", "value": "{DAYS_LEFT}"}]
    try:
        asyncio.run(_store_admin_variables(admin["username"], variables))
        path = f"/api/admin/by-id/{admin['id']}" if route == "by_id" else f"/api/admin/{admin['username']}"
        response = client.put(path, headers=headers, json=_toggle_payload(admin, variables))
        assert response.status_code == status.HTTP_200_OK, response.text
        assert response.json()["status"] == "disabled"
        assert asyncio.run(_stored_admin_variables(admin["username"])) == variables
    finally:
        delete_admin(access_token, admin["username"])


@pytest.mark.parametrize("value", LEGACY + OLDER_LEGACY)
def test_a_settings_save_with_an_unrelated_change_keeps_a_stored_legacy_value(access_token, value):
    headers = auth_headers(access_token)
    before = client.get("/api/settings", headers=headers).json()
    variables = [{"key": "PROMO", "value": value}]
    asyncio.run(_store_subscription_variables(variables))
    try:
        loaded = client.get("/api/settings", headers=headers).json()["subscription"]
        assert loaded["custom_variables"] == variables
        body = {**loaded, "profile_title": unique_name("title")}
        response = client.put("/api/settings", headers=headers, json={"subscription": body})
        assert response.status_code == status.HTTP_200_OK, response.text
        assert response.json()["subscription"]["custom_variables"] == variables
    finally:
        asyncio.run(_store_subscription_variables(before["subscription"]["custom_variables"]))
        client.put("/api/settings", headers=headers, json={"subscription": before["subscription"]})


@pytest.mark.parametrize("stored", ["{DAYS_LEFT}", None], ids=["changed", "new"])
@pytest.mark.parametrize("value", LEGACY + OLDER_LEGACY)
def test_a_new_or_changed_invalid_value_is_refused_naming_its_key(access_token, value, stored):
    headers = auth_headers(access_token)
    admin = create_admin(access_token)
    before = client.get("/api/settings", headers=headers).json()
    previous = [] if stored is None else [{"key": "PROMO", "value": stored}]
    try:
        asyncio.run(_store_admin_variables(admin["username"], previous))
        asyncio.run(_store_subscription_variables(previous))
        variables = [{"key": "PROMO", "value": value}]
        refused_admin = client.put(
            f"/api/admin/by-id/{admin['id']}", headers=headers, json=_toggle_payload(admin, variables)
        )
        loaded = client.get("/api/settings", headers=headers).json()["subscription"]
        refused_settings = client.put(
            "/api/settings", headers=headers, json={"subscription": {**loaded, "custom_variables": variables}}
        )
        for response in (refused_admin, refused_settings):
            assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, response.text
            assert "PROMO" in str(response.json()["detail"])
        assert asyncio.run(_stored_admin_variables(admin["username"])) == previous
    finally:
        delete_admin(access_token, admin["username"])
        asyncio.run(_store_subscription_variables(before["subscription"]["custom_variables"]))
        client.put("/api/settings", headers=headers, json={"subscription": before["subscription"]})


def test_a_new_admin_with_an_invalid_value_is_refused_naming_its_key(access_token):
    response = client.post(
        "/api/admin",
        headers=auth_headers(access_token),
        json={
            "username": unique_name("legacy_new"),
            "password": "TestAdmincreate#12Ab",
            "role_id": 3,
            "custom_variables": [{"key": "PROMO", "value": "{USERNAME:d}"}],
        },
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, response.text
    assert "PROMO" in str(response.json()["detail"])


def _login(username: str, password: str) -> str:
    response = client.post(
        "/api/admin/token", data={"username": username, "password": password, "grant_type": "password"}
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    return response.json()["access_token"]


def test_a_subscription_of_a_user_whose_admin_holds_legacy_values_still_renders(access_token):
    headers = auth_headers(access_token)
    owner = create_admin(access_token, role_id=2)
    core = create_core(access_token, name=unique_name("legacy_core"))
    inbound = client.get("/api/inbounds", headers=headers).json()[0]
    host = client.post(
        "/api/host",
        headers=headers,
        json={
            "remark": "a-{PROMO}|b-{OLD}|c-{BIG}",
            "address": ["127.0.0.1"],
            "port": 443,
            "inbound_tag": inbound,
            "priority": 1,
        },
    )
    assert host.status_code == status.HTTP_201_CREATED, host.text
    group = create_group(access_token, name=unique_name("legacy_group"))
    expire = (datetime.now(UTC) + timedelta(days=3)).isoformat()
    owner_headers = auth_headers(_login(owner["username"], owner["password"]))
    users = []
    try:
        for name, payload in (("unl", {"data_limit": 0}), ("lim", {"data_limit": 1024**3, "expire": expire})):
            created = client.post(
                "/api/user",
                headers=owner_headers,
                json={
                    "username": unique_name(f"legacy_{name}"),
                    "proxy_settings": {},
                    "data_limit_reset_strategy": "no_reset",
                    "status": "active",
                    "group_ids": [group["id"]],
                    **payload,
                },
            )
            assert created.status_code == status.HTTP_201_CREATED, created.text
            users.append(created.json())
        asyncio.run(
            _store_admin_variables(
                owner["username"],
                [
                    {"key": "PROMO", "value": "{DAYS_LEFT:03d}"},
                    {"key": "OLD", "value": "{USERNAME[0]}"},
                    {"key": "BIG", "value": "{USERNAME:>300}"},
                ],
            )
        )
        remarks = []
        for user in users:
            served = client.get(f"{user['subscription_url']}/links")
            assert served.status_code == status.HTTP_200_OK, served.text
            remarks += [unquote(line.rsplit("#", 1)[1]) for line in served.text.splitlines() if "#a-" in line]
    finally:
        for user in users:
            client.delete(f"/api/user/{user['username']}", headers=headers)
        client.delete(f"/api/host/{host.json()['id']}", headers=headers)
        delete_group(access_token, group["id"])
        delete_core(access_token, core["id"])
        delete_admin(access_token, owner["username"])

    assert sorted(remarks) == [
        "a-003|b-{USERNAME[0]}|c-{USERNAME:>300}",
        "a-{DAYS_LEFT:03d}|b-{USERNAME[0]}|c-{USERNAME:>300}",
    ]
