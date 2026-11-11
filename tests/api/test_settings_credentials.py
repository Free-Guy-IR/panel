import asyncio
import secrets
import string

import pytest
from fastapi import status
from sqlalchemy import delete, select

from app.db.models import Admin, Settings
from app.models.admin import hash_password
from app.operation.settings import SettingsOperation
from tests.api import TestSession, client
from tests.api.concurrent_requests import send_concurrently
from tests.api.helpers import auth_headers, create_admin, delete_admin, strong_password, unique_name

CREDENTIAL_SECTIONS = ("telegram", "notification_settings", "webhook")
KEY_HEADER_FORMS = ("X-Api-Key", "Authorization")


def _bot_token() -> str:
    alphabet = string.ascii_letters + string.digits
    return f"{secrets.randbelow(9 * 10**8) + 10**8}:" + "".join(secrets.choice(alphabet) for _ in range(35))


def _telegram(token: str) -> dict:
    return {
        "enable": False,
        "token": token,
        "webhook_url": "",
        "webhook_secret": "hook-secret-value",
        "proxy_url": None,
        "method": "webhook",
        "mini_app_login": True,
        "mini_app_web_url": "",
        "for_admins_only": True,
    }


def _key_headers(raw_key: str, form: str) -> dict[str, str]:
    return {"X-Api-Key": raw_key} if form == "X-Api-Key" else {"Authorization": f"ApiKey {raw_key}"}


def _login(username: str, password: str) -> str:
    response = client.post(
        "/api/admin/token", data={"username": username, "password": password, "grant_type": "password"}
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    return response.json()["access_token"]


def _inheriting_key(session_token: str) -> str:
    response = client.post(
        "/api/api_key",
        headers=auth_headers(session_token),
        json={"name": unique_name("settings_key"), "inherit_permissions": True},
    )
    assert response.status_code == status.HTTP_201_CREATED, response.text
    return response.json()["api_key"]


async def _stored_sections() -> dict:
    async with TestSession() as db:
        row = (await db.execute(select(Settings))).scalars().first()
        return {section: getattr(row, section) for section in CREDENTIAL_SECTIONS}


class OwnerRow:
    def __init__(self):
        self.username = unique_name("settings_owner")
        self.password = strong_password("SettingsOwner")

        async def _insert() -> int:
            async with TestSession() as db:
                row = Admin(username=self.username, hashed_password=await hash_password(self.password), role_id=1)
                db.add(row)
                await db.commit()
                return row.id

        self.id = asyncio.run(_insert())
        self.session = _login(self.username, self.password)
        self.key = _inheriting_key(self.session)

    def close(self) -> None:
        async def _delete() -> None:
            async with TestSession() as db:
                await db.execute(delete(Admin).where(Admin.username == self.username))
                await db.commit()

        asyncio.run(_delete())


@pytest.fixture
def quiet_services(monkeypatch):
    async def no_reset(old_settings, new_settings):
        return None

    monkeypatch.setattr(SettingsOperation, "reset_services", staticmethod(no_reset))


@pytest.fixture
def stored_credentials(access_token, quiet_services):
    original = client.get("/api/settings", headers=auth_headers(access_token)).json()
    token = _bot_token()
    response = client.put("/api/settings", headers=auth_headers(access_token), json={"telegram": _telegram(token)})
    assert response.status_code == status.HTTP_200_OK, response.text
    yield token
    restore = {section: original[section] for section in CREDENTIAL_SECTIONS if original[section] is not None}
    if restore:
        client.put("/api/settings", headers=auth_headers(access_token), json=restore)


@pytest.fixture
def owner_row():
    owner = OwnerRow()
    yield owner
    owner.close()


@pytest.fixture
def administrator(access_token):
    admin = create_admin(access_token, role_id=2)
    admin["session"] = _login(admin["username"], admin["password"])
    yield admin
    delete_admin(access_token, admin["username"])


def test_the_owner_on_a_session_reads_the_credential_sections(access_token, stored_credentials, owner_row):
    for token in (access_token, owner_row.session):
        body = client.get("/api/settings", headers=auth_headers(token)).json()
        assert body["telegram"]["token"] == stored_credentials
        assert body["notification_settings"] is not None
        assert body["webhook"] is not None


@pytest.mark.parametrize("form", KEY_HEADER_FORMS)
def test_the_owners_api_key_reads_the_settings_without_credential_sections(stored_credentials, owner_row, form):
    response = client.get("/api/settings", headers=_key_headers(owner_row.key, form))
    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    for section in CREDENTIAL_SECTIONS:
        assert body[section] is None
    assert stored_credentials not in response.text
    assert body["general"] is not None
    assert body["subscription"] is not None


def test_a_non_owner_admin_reads_the_settings_without_credential_sections(stored_credentials, administrator):
    response = client.get("/api/settings", headers=auth_headers(administrator["session"]))
    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    for section in CREDENTIAL_SECTIONS:
        assert body[section] is None
    assert stored_credentials not in response.text


@pytest.mark.parametrize("form", KEY_HEADER_FORMS)
def test_an_api_key_cannot_change_the_bot_token(stored_credentials, owner_row, form):
    before = asyncio.run(_stored_sections())
    response = client.put(
        "/api/settings", headers=_key_headers(owner_row.key, form), json={"telegram": _telegram(_bot_token())}
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN, response.text
    assert asyncio.run(_stored_sections()) == before


@pytest.mark.parametrize("section", CREDENTIAL_SECTIONS)
def test_an_api_key_cannot_change_any_credential_section(access_token, stored_credentials, owner_row, section):
    before = asyncio.run(_stored_sections())
    current = client.get("/api/settings", headers=auth_headers(access_token)).json()[section]
    response = client.put("/api/settings", headers={"X-Api-Key": owner_row.key}, json={section: current})
    assert response.status_code == status.HTTP_403_FORBIDDEN, response.text
    assert asyncio.run(_stored_sections()) == before


@pytest.mark.parametrize("section", CREDENTIAL_SECTIONS)
def test_a_non_owner_admin_cannot_change_a_credential_section(access_token, stored_credentials, administrator, section):
    before = asyncio.run(_stored_sections())
    current = client.get("/api/settings", headers=auth_headers(access_token)).json()[section]
    response = client.put("/api/settings", headers=auth_headers(administrator["session"]), json={section: current})
    assert response.status_code == status.HTTP_403_FORBIDDEN, response.text
    assert asyncio.run(_stored_sections()) == before


def test_a_non_owner_round_trip_keeps_the_credential_sections(stored_credentials, administrator):
    before = asyncio.run(_stored_sections())
    seen = client.get("/api/settings", headers=auth_headers(administrator["session"])).json()
    saved = client.put("/api/settings", headers=auth_headers(administrator["session"]), json=seen)
    assert saved.status_code == status.HTTP_200_OK, saved.text
    for section in CREDENTIAL_SECTIONS:
        assert saved.json()[section] is None
    assert stored_credentials not in saved.text
    assert asyncio.run(_stored_sections()) == before


def test_a_key_save_of_other_sections_works_and_returns_no_credentials(stored_credentials, owner_row):
    general = client.get("/api/settings", headers={"X-Api-Key": owner_row.key}).json()["general"]
    saved = client.put("/api/settings", headers={"X-Api-Key": owner_row.key}, json={"general": general})
    assert saved.status_code == status.HTTP_200_OK, saved.text
    for section in CREDENTIAL_SECTIONS:
        assert saved.json()[section] is None
    assert stored_credentials not in saved.text


def test_an_invalid_session_next_to_a_valid_key_cannot_change_the_bot_token(stored_credentials, owner_row):
    before = asyncio.run(_stored_sections())
    response = client.put(
        "/api/settings",
        headers={"Authorization": "Bearer not-a-real-token", "X-Api-Key": owner_row.key},
        json={"telegram": _telegram(_bot_token())},
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN, response.text
    assert asyncio.run(_stored_sections()) == before


def test_the_owner_on_a_session_changes_the_bot_token(stored_credentials, owner_row):
    token = _bot_token()
    response = client.put("/api/settings", headers=auth_headers(owner_row.session), json={"telegram": _telegram(token)})
    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["telegram"]["token"] == token
    assert asyncio.run(_stored_sections())["telegram"]["token"] == token


def test_a_key_read_paused_across_an_owner_token_change_returns_neither_token(
    stored_credentials, owner_row, monkeypatch
):
    new_token = _bot_token()
    row_read = asyncio.Event()
    token_changed = asyncio.Event()
    real_read = SettingsOperation.get_settings
    real_change = SettingsOperation.modify_settings

    async def read_then_wait(self, db):
        row = await real_read(self, db)
        row_read.set()
        await asyncio.wait_for(token_changed.wait(), timeout=30)
        return row

    async def change_after_the_read(self, db, modify):
        await asyncio.wait_for(row_read.wait(), timeout=30)
        try:
            return await real_change(self, db, modify)
        finally:
            token_changed.set()

    monkeypatch.setattr(SettingsOperation, "get_settings", read_then_wait)
    monkeypatch.setattr(SettingsOperation, "modify_settings", change_after_the_read)
    read, changed = send_concurrently(
        [
            ("GET", "/api/settings", {"headers": {"X-Api-Key": owner_row.key}}),
            (
                "PUT",
                "/api/settings",
                {"headers": auth_headers(owner_row.session), "json": {"telegram": _telegram(new_token)}},
            ),
        ]
    )
    assert changed.status_code == status.HTTP_200_OK, changed.text
    assert read.status_code == status.HTTP_200_OK, read.text
    assert read.json()["telegram"] is None
    assert stored_credentials not in read.text
    assert new_token not in read.text
    assert asyncio.run(_stored_sections())["telegram"]["token"] == new_token


def test_an_owner_token_change_racing_key_saves_reads_and_swaps_keeps_both_and_leaks_nothing(
    stored_credentials, owner_row
):
    new_token = _bot_token()
    general = client.get("/api/settings", headers={"X-Api-Key": owner_row.key}).json()["general"]
    key = {"X-Api-Key": owner_row.key}
    calls = [
        (
            "PUT",
            "/api/settings",
            {"headers": auth_headers(owner_row.session), "json": {"telegram": _telegram(new_token)}},
        )
    ]
    calls += [("PUT", "/api/settings", {"headers": key, "json": {"general": general}}) for _ in range(3)]
    calls += [("GET", "/api/settings", {"headers": key}) for _ in range(4)]
    calls += [
        ("PUT", "/api/settings", {"headers": key, "json": {"telegram": _telegram(_bot_token())}}) for _ in range(3)
    ]

    responses = send_concurrently(calls)
    owner_change, saves, reads, swaps = responses[0], responses[1:4], responses[4:8], responses[8:]

    assert owner_change.status_code == status.HTTP_200_OK, owner_change.text
    for response in saves + reads:
        assert response.status_code == status.HTTP_200_OK, response.text
        assert response.json()["telegram"] is None
        assert new_token not in response.text
        assert stored_credentials not in response.text
    for response in swaps:
        assert response.status_code == status.HTTP_403_FORBIDDEN, response.text
    assert asyncio.run(_stored_sections())["telegram"]["token"] == new_token
