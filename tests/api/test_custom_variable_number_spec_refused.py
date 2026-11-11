import asyncio

import pytest
from fastapi import status
from sqlalchemy import select

from app.db.crud.settings import get_settings
from app.db.models import Admin
from tests.api import GetTestDB, client
from tests.api.helpers import auth_headers, create_admin, delete_admin

NUMBER_SPECS = [
    "{USERNAME:d}",
    "{USERNAME:,.2f}",
    "{DAYS_LEFT:,}",
    "{DAYS_LEFT:03d}",
    "{USAGE_PERCENTAGE:.1f}",
    "{DAYS_LEFT:.2}",
    "{DAYS_LEFT:s}",
    "{USAGE_PERCENTAGE:s}",
]
STRING_VALUES = ["{DAYS_LEFT}", "{USERNAME:>10}", "{DAYS_LEFT:>4}", "{USAGE_PERCENTAGE:.3}"]
STORED = "{DAYS_LEFT:,}"


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


def _put_settings(headers: dict, before: dict, section: str, value: str):
    body = {**before[section], "custom_variables": [{"key": "PROMO", "value": value}]}
    return client.put("/api/settings", headers=headers, json={section: body})


@pytest.mark.parametrize("value", NUMBER_SPECS)
@pytest.mark.parametrize("section", ["subscription", "general"])
def test_the_settings_api_refuses_a_number_format(access_token, section, value):
    headers = auth_headers(access_token)
    before = client.get("/api/settings", headers=headers).json()
    response = _put_settings(headers, before, section, value)
    try:
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, response.text
    finally:
        client.put("/api/settings", headers=headers, json={section: before[section]})


@pytest.mark.parametrize("value", NUMBER_SPECS)
def test_the_admin_api_refuses_a_number_format(access_token, value):
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


@pytest.mark.parametrize("value", STRING_VALUES)
@pytest.mark.parametrize("section", ["subscription", "general"])
def test_the_settings_api_still_saves_a_string_format(access_token, section, value):
    headers = auth_headers(access_token)
    before = client.get("/api/settings", headers=headers).json()
    response = _put_settings(headers, before, section, value)
    try:
        assert response.status_code == status.HTTP_200_OK, response.text
        assert response.json()[section]["custom_variables"] == [{"key": "PROMO", "value": value}]
    finally:
        client.put("/api/settings", headers=headers, json={section: before[section]})


@pytest.mark.parametrize("value", STRING_VALUES)
def test_the_admin_api_still_saves_a_string_format(access_token, value):
    headers = auth_headers(access_token)
    admin = create_admin(access_token)
    try:
        response = client.put(
            f"/api/admin/{admin['username']}",
            headers=headers,
            json={"custom_variables": [{"key": "PROMO", "value": value}]},
        )
        assert response.status_code == status.HTTP_200_OK, response.text
    finally:
        delete_admin(access_token, admin["username"])


def test_settings_holding_a_stored_number_format_still_load(access_token):
    headers = auth_headers(access_token)
    before = client.get("/api/settings", headers=headers).json()
    asyncio.run(_store_subscription_variables([{"key": "PROMO", "value": STORED}]))
    try:
        response = client.get("/api/settings", headers=headers)
        assert response.status_code == status.HTTP_200_OK, response.text
        assert response.json()["subscription"]["custom_variables"] == [{"key": "PROMO", "value": STORED}]
    finally:
        asyncio.run(_store_subscription_variables(before["subscription"]["custom_variables"]))


def test_an_admin_holding_a_stored_number_format_still_loads(access_token):
    headers = auth_headers(access_token)
    admin = create_admin(access_token)
    try:
        asyncio.run(_store_admin_variables(admin["username"], [{"key": "PROMO", "value": STORED}]))
        response = client.get("/api/admins", headers=headers, params={"username": admin["username"]})
        assert response.status_code == status.HTTP_200_OK, response.text
        listed = [item for item in response.json()["admins"] if item["username"] == admin["username"]]
        assert listed[0]["custom_variables"] == [{"key": "PROMO", "value": STORED}]
    finally:
        delete_admin(access_token, admin["username"])
