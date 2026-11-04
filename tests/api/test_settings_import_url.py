import logging

import pytest
from sqlalchemy import select, update

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


LEGACY_SCHEME_URL = "oldclient://install/{url}"


async def _stored_import_urls() -> list[str]:
    async with TestSession() as db:
        row = (await db.execute(select(Settings))).scalars().first()
        return [app.get("import_url") for app in row.subscription["applications"]]


@pytest.mark.asyncio
async def test_a_dashboard_save_of_the_loaded_section_keeps_a_legacy_import_url(access_token):
    original = client.get("/api/settings", headers=auth_headers(access_token)).json()["subscription"]
    stored_mock = settings_module.get_settings.return_value
    mocked_original = stored_mock.subscription
    legacy_app = {**_with_import_url(original, LEGACY_SCHEME_URL)["applications"][0], "name": LEGACY_APP}
    valid_app = {**_with_import_url(original, "happ://add/{url}")["applications"][0], "name": "valid-client"}
    legacy = {**original, "applications": [legacy_app, valid_app]}
    async with TestSession() as db:
        await db.execute(update(Settings).values(subscription=legacy))
        await db.commit()
    stored_mock.subscription = {**mocked_original, "applications": legacy["applications"]}
    await settings_module.refresh_caches()
    user = create_user(access_token, payload={"username": unique_name("legacy_keep"), "hwid_limit": 0})
    try:
        loaded = client.get("/api/settings", headers=auth_headers(access_token)).json()["subscription"]
        assert [app["import_url"] for app in loaded["applications"]] == ["", "happ://add/{url}"]

        edited = {
            **loaded,
            "applications": [loaded["applications"][0], {**loaded["applications"][1], "recommended": True}],
        }
        saved = client.put("/api/settings", headers=auth_headers(access_token), json={"subscription": edited})
        assert saved.status_code == 200, saved.text
        assert [app["import_url"] for app in saved.json()["subscription"]["applications"]] == ["", "happ://add/{url}"]
        assert saved.json()["subscription"]["applications"][1]["recommended"] is True
        assert await _stored_import_urls() == [LEGACY_SCHEME_URL, "happ://add/{url}"]

        reordered = {**loaded, "applications": list(reversed(loaded["applications"]))}
        saved_again = client.put("/api/settings", headers=auth_headers(access_token), json={"subscription": reordered})
        assert saved_again.status_code == 200, saved_again.text
        assert await _stored_import_urls() == ["happ://add/{url}", LEGACY_SCHEME_URL]

        for hostile in ("oldclient2://install/{url}", "javascript:alert(1)//{url}", "data:text/html,{url}"):
            injected = {
                **loaded,
                "applications": [{**loaded["applications"][0], "import_url": hostile}, loaded["applications"][1]],
            }
            refused = client.put("/api/settings", headers=auth_headers(access_token), json={"subscription": injected})
            assert refused.status_code == 422, refused.text
        assert await _stored_import_urls() == ["happ://add/{url}", LEGACY_SCHEME_URL]

        stored_mock.subscription = {
            **mocked_original,
            "applications": list(reversed(legacy["applications"])),
        }
        await settings_module.refresh_caches()
        page = client.get(user["subscription_url"], headers={"Accept": "text/html"})
        apps = client.get(f"{user['subscription_url']}/apps")
        assert page.status_code == 200
        assert "oldclient" not in page.text.lower()
        assert apps.status_code == 200
        assert "oldclient" not in apps.text.lower()
        import_urls = {app["name"]: app["import_url"] for app in apps.json()}
        assert import_urls[LEGACY_APP] == ""
        assert import_urls["valid-client"].startswith("happ://add/")

        general = client.get("/api/settings", headers=auth_headers(access_token)).json()["general"]
        general = {**general, "custom_variables": original.get("custom_variables") or []}
        general_saved = client.put("/api/settings", headers=auth_headers(access_token), json={"general": general})
        assert general_saved.status_code == 200, general_saved.text
        assert await _stored_import_urls() == ["happ://add/{url}", LEGACY_SCHEME_URL]

        replaced = {
            **loaded,
            "applications": [{**loaded["applications"][0], "import_url": "v2rayng://install-config?url={url}"}],
        }
        repaired = client.put("/api/settings", headers=auth_headers(access_token), json={"subscription": replaced})
        assert repaired.status_code == 200, repaired.text
        assert await _stored_import_urls() == ["v2rayng://install-config?url={url}"]
    finally:
        delete_user(access_token, user["username"])
        stored_mock.subscription = mocked_original
        restored = client.put("/api/settings", headers=auth_headers(access_token), json={"subscription": original})
        assert restored.status_code == 200, restored.text
        await settings_module.refresh_caches()
