from tests.api import client
from tests.api.helpers import auth_headers


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
