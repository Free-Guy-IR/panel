import logging

import pytest
from sqlalchemy import update

import app.settings as settings_module
from app.db.models import Settings
from tests.api import TestSession, client
from tests.api.helpers import auth_headers, create_user, delete_user, unique_name


def _with_import_url(subscription: dict, import_url: str) -> dict:
    application = {
        "name": "client",
        "platform": "android",
        "download_links": [],
        "import_url": import_url,
    }
    return {**subscription, "applications": [application]}


def test_settings_refuse_a_hostile_import_url_and_keep_the_stored_one(access_token):
    original = client.get("/api/settings", headers=auth_headers(access_token)).json()["subscription"]
    try:
        refused = client.put(
            "/api/settings",
            headers=auth_headers(access_token),
            json={"subscription": _with_import_url(original, "\x01javascript:alert(1)//{url}")},
        )
        assert refused.status_code == 422, refused.text
        stored = client.get("/api/settings", headers=auth_headers(access_token)).json()["subscription"]
        assert stored["applications"] == original["applications"]

        accepted = client.put(
            "/api/settings",
            headers=auth_headers(access_token),
            json={"subscription": _with_import_url(original, "happ://add/{url}")},
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["subscription"]["applications"][0]["import_url"] == "happ://add/{url}"
    finally:
        restored = client.put("/api/settings", headers=auth_headers(access_token), json={"subscription": original})
        assert restored.status_code == 200, restored.text


HOSTILE = "\x01javascript:alert(document.domain)//{url}"
LEGACY_APP = "legacy-client"


@pytest.mark.asyncio
async def test_a_stored_invalid_import_url_still_loads_hides_its_button_and_can_only_be_replaced(access_token, caplog):
    original = client.get("/api/settings", headers=auth_headers(access_token)).json()["subscription"]
    stored_mock = settings_module.get_settings.return_value
    mocked_original = stored_mock.subscription
    legacy = {
        **original,
        "applications": [{**_with_import_url(original, HOSTILE)["applications"][0], "name": LEGACY_APP}],
    }
    async with TestSession() as db:
        await db.execute(update(Settings).values(subscription=legacy))
        await db.commit()
    stored_mock.subscription = {**mocked_original, "applications": legacy["applications"]}
    await settings_module.refresh_caches()
    user = create_user(access_token, payload={"username": unique_name("legacy_app"), "hwid_limit": 0})
    try:
        with caplog.at_level(logging.WARNING):
            loaded = client.get("/api/settings", headers=auth_headers(access_token))
            page = client.get(user["subscription_url"], headers={"Accept": "text/html"})
            apps = client.get(f"{user['subscription_url']}/apps")
        assert loaded.status_code == 200, loaded.text
        assert loaded.json()["subscription"]["applications"][0]["import_url"] == ""
        assert page.status_code == 200
        assert 'data-url="' not in page.text
        assert "javascript" not in page.text.lower()
        assert apps.status_code == 200
        assert [app["import_url"] for app in apps.json()] == [""]
        warnings = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
        assert any(LEGACY_APP in message for message in warnings), warnings
        assert not any("javascript" in message.lower() for message in warnings)

        kept = client.put("/api/settings", headers=auth_headers(access_token), json={"subscription": legacy})
        assert kept.status_code == 422, kept.text

        fixed = {**legacy, "applications": [{**legacy["applications"][0], "import_url": "happ://add/{url}"}]}
        repaired = client.put("/api/settings", headers=auth_headers(access_token), json={"subscription": fixed})
        assert repaired.status_code == 200, repaired.text
        assert repaired.json()["subscription"]["applications"][0]["import_url"] == "happ://add/{url}"
    finally:
        delete_user(access_token, user["username"])
        stored_mock.subscription = mocked_original
        restored = client.put("/api/settings", headers=auth_headers(access_token), json={"subscription": original})
        assert restored.status_code == 200, restored.text
        await settings_module.refresh_caches()
