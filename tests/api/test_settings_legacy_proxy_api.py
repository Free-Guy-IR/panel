import asyncio

import pytest
from fastapi import status
from sqlalchemy import select

from app.db.models import Settings
from app.settings import refresh_caches
from tests.api import TestSession, client
from tests.api.helpers import auth_headers

SECRET = "pw" + "Leak" + "Marker" + str(7)
HOST = "proxy.example"
SECTIONS = ("telegram", "webhook", "notification_settings")
LEGACY_PROXIES = [
    f"http://ux:{SECRET}#x@{HOST}:8080",
    f"http://ux:{SECRET}@{HOST}:99999",
    f"http://ux:{SECRET}@{HOST}:8080\n",
]


def _rewrite_sections(update) -> dict:
    async def _run():
        async with TestSession() as session:
            row = (await session.execute(select(Settings))).scalar_one()
            original = {section: getattr(row, section) for section in SECTIONS}
            for section, value in update(original).items():
                setattr(row, section, value)
            await session.commit()
        await refresh_caches()
        return original

    return asyncio.run(_run())


@pytest.mark.parametrize("proxy", LEGACY_PROXIES)
def test_settings_api_with_a_legacy_stored_proxy(access_token, proxy):
    original = _rewrite_sections(
        lambda current: {section: {**current[section], "proxy_url": proxy} for section in SECTIONS}
    )
    try:
        loaded = client.get("/api/settings", headers=auth_headers(access_token))
        assert loaded.status_code == status.HTTP_200_OK, loaded.text
        body = loaded.json()
        for section in SECTIONS:
            assert body[section]["proxy_url"] == proxy

        untouched = client.put("/api/settings", headers=auth_headers(access_token), json={"general": body["general"]})
        assert untouched.status_code == status.HTTP_200_OK, untouched.text
        for section in SECTIONS:
            assert untouched.json()[section]["proxy_url"] == proxy

        for section in SECTIONS:
            echoed = client.put("/api/settings", headers=auth_headers(access_token), json={section: body[section]})
            assert echoed.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, section
            assert SECRET not in echoed.text

        replaced = client.put(
            "/api/settings",
            headers=auth_headers(access_token),
            json={"notification_settings": {**body["notification_settings"], "proxy_url": f"socks5://{HOST}:1080"}},
        )
        assert replaced.status_code == status.HTTP_200_OK, replaced.text
        assert replaced.json()["notification_settings"]["proxy_url"] == f"socks5://{HOST}:1080"
        assert replaced.json()["webhook"]["proxy_url"] == proxy
    finally:
        _rewrite_sections(lambda current: original)
