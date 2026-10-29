import asyncio

import pytest
from fastapi import status
from sqlalchemy import select

from app.db.crud.settings import get_settings
from app.db.models import Admin
from tests.api import GetTestDB, client
from tests.api.helpers import auth_headers, create_admin, delete_admin

LEGACY = "{USERNAME:>300}"
REFUSED = ["{NOPE}{USERNAME[0]}", "{USERNAME.__class__.__dict__}", "{NOPE}{USERNAME:>300}"]


async def _store_subscription_variables(variables: list[dict]) -> None:
    async with GetTestDB() as db:
        db_settings = await get_settings(db)
        db_settings.subscription = {**db_settings.subscription, "custom_variables": variables}
        await db.commit()


async def _store_admin_variables(username: str, variables: list[dict]) -> None:
    async with GetTestDB() as db:
        admin = (await db.execute(select(Admin).where(Admin.username == username))).scalar_one()
        admin.custom_variables = variables
        await db.commit()


@pytest.mark.parametrize("value", REFUSED)
@pytest.mark.parametrize("section", ["subscription", "general"])
def test_the_settings_api_refuses_a_custom_variable_it_cannot_format(access_token, section, value):
    headers = auth_headers(access_token)
    before = client.get("/api/settings", headers=headers).json()
    body = {**before[section], "custom_variables": [{"key": "PROMO", "value": value}]}
    response = client.put("/api/settings", headers=headers, json={section: body})
    try:
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, response.text
        assert client.get("/api/settings", headers=headers).json()["subscription"] == before["subscription"]
    finally:
        client.put("/api/settings", headers=headers, json={"subscription": before["subscription"]})


@pytest.mark.parametrize("value", REFUSED)
def test_the_admin_api_refuses_a_custom_variable_it_cannot_format(access_token, value):
    headers = auth_headers(access_token)
    admin = create_admin(access_token)
    try:
        response = client.put(
            f"/api/admin/{admin['username']}",
            headers=headers,
            json={"custom_variables": [{"key": "PROMO", "value": value}]},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, response.text
    finally:
        delete_admin(access_token, admin["username"])


def test_settings_holding_a_custom_variable_saved_before_the_strict_check_still_load(access_token):
    headers = auth_headers(access_token)
    before = client.get("/api/settings", headers=headers).json()
    asyncio.run(_store_subscription_variables([{"key": "PROMO", "value": LEGACY}]))
    try:
        response = client.get("/api/settings", headers=headers)
        assert response.status_code == status.HTTP_200_OK, response.text
        assert response.json()["subscription"]["custom_variables"] == [{"key": "PROMO", "value": LEGACY}]
        general = client.get("/api/settings/general", headers=headers)
        assert general.status_code == status.HTTP_200_OK, general.text
    finally:
        asyncio.run(_store_subscription_variables(before["subscription"]["custom_variables"]))


def test_an_admin_holding_a_custom_variable_saved_before_the_strict_check_still_loads(access_token):
    headers = auth_headers(access_token)
    admin = create_admin(access_token)
    try:
        asyncio.run(_store_admin_variables(admin["username"], [{"key": "PROMO", "value": LEGACY}]))
        response = client.get("/api/admins", headers=headers, params={"username": admin["username"]})
        assert response.status_code == status.HTTP_200_OK, response.text
        listed = [item for item in response.json()["admins"] if item["username"] == admin["username"]]
        assert listed[0]["custom_variables"] == [{"key": "PROMO", "value": LEGACY}]
    finally:
        delete_admin(access_token, admin["username"])
