import asyncio
import importlib.util
from pathlib import Path

from fastapi import status
from sqlalchemy import select

from app.db.models import User
from tests.api import TestSession, client, engine
from tests.api.helpers import auth_headers, create_user, delete_user

MASQUE_MIGRATION = Path("app/db/migrations/versions/c8d2e4f6a1b3_add_masque_settings_to_proxy_settings.py")


def _load_masque_migration():
    spec = importlib.util.spec_from_file_location("masque_proxy_settings_migration", MASQUE_MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _stored(username: str) -> dict:
    async with TestSession() as db:
        return await db.scalar(select(User.proxy_settings).where(User.username == username))


def _stored_settings(username: str) -> dict:
    return asyncio.run(_stored(username))


async def _store_without_masque(username: str) -> None:
    async with TestSession() as db:
        user = await db.scalar(select(User).where(User.username == username))
        user.proxy_settings = {key: value for key, value in user.proxy_settings.items() if key != "masque"}
        await db.commit()


def _run_masque_migration(monkeypatch) -> None:
    module = _load_masque_migration()

    def upgrade(connection):
        monkeypatch.setattr(module.op, "get_bind", lambda: connection)
        module.upgrade()

    async def run():
        async with engine.begin() as connection:
            await connection.run_sync(upgrade)

    asyncio.run(run())


def _user_from_before_the_migration(access_token, monkeypatch) -> tuple[str, str]:
    username = create_user(access_token)["username"]
    asyncio.run(_store_without_masque(username))
    assert "masque" not in _stored_settings(username)
    _run_masque_migration(monkeypatch)
    password = _stored_settings(username)["masque"]["password"]
    assert password
    return username, password


def _put(access_token, username, body):
    response = client.put(f"/api/user/{username}", headers=auth_headers(access_token), json=body)
    assert response.status_code == status.HTTP_200_OK, response.text
    return response.json()


def test_migrated_masque_password_survives_a_dashboard_edit(access_token, monkeypatch):
    username, password = _user_from_before_the_migration(access_token, monkeypatch)
    try:
        shown = client.get(f"/api/user/{username}", headers=auth_headers(access_token)).json()

        _put(access_token, username, {"proxy_settings": shown["proxy_settings"], "note": "edited"})

        assert _stored_settings(username)["masque"]["password"] == password
        assert shown["proxy_settings"]["masque"]["password"] == password
    finally:
        delete_user(access_token, username)


def test_migrated_masque_password_survives_edits_without_proxy_settings(access_token, monkeypatch):
    username, password = _user_from_before_the_migration(access_token, monkeypatch)
    try:
        _put(access_token, username, {"note": "edited"})
        _put(access_token, username, {"data_limit": 2 * 1024 * 1024})

        assert _stored_settings(username)["masque"]["password"] == password
        shown = client.get(f"/api/user/{username}", headers=auth_headers(access_token)).json()
        assert shown["proxy_settings"]["masque"]["password"] == password
    finally:
        delete_user(access_token, username)


def test_new_user_gets_a_stable_masque_password(access_token):
    username = create_user(access_token)["username"]
    try:
        password = _stored_settings(username)["masque"]["password"]
        assert password

        _put(access_token, username, {"note": "edited"})
        shown = client.get(f"/api/user/{username}", headers=auth_headers(access_token)).json()

        assert shown["proxy_settings"]["masque"]["password"] == password
        assert _stored_settings(username)["masque"]["password"] == password
    finally:
        delete_user(access_token, username)


def test_masque_migration_keeps_a_stored_password_and_fills_an_empty_one(access_token, monkeypatch):
    kept = create_user(access_token)["username"]
    emptied = create_user(access_token)["username"]
    try:
        kept_password = _stored_settings(kept)["masque"]["password"]

        async def empty_password():
            async with TestSession() as db:
                user = await db.scalar(select(User).where(User.username == emptied))
                user.proxy_settings = {**user.proxy_settings, "masque": {"password": ""}}
                await db.commit()

        asyncio.run(empty_password())
        _run_masque_migration(monkeypatch)

        assert _stored_settings(kept)["masque"]["password"] == kept_password
        assert _stored_settings(emptied)["masque"]["password"]
    finally:
        delete_user(access_token, kept)
        delete_user(access_token, emptied)
