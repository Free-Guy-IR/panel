import asyncio
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi import status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.crud.admin import get_admin as get_admin_by_username
from app.db.models import Admin
from app.models.admin import hash_password
from app.routers.authentication import get_admin, get_admin_with_metrics
from app.utils.jwt import get_secret_key
from tests.api import DATABASE_URL, IS_SQLITE, TestSession, client
from tests.api.helpers import auth_headers, create_admin, delete_admin, strong_password, unique_name


def _login(username: str, password: str) -> str:
    response = client.post(
        "/api/admin/token",
        data={"username": username, "password": password, "grant_type": "password"},
    )
    assert response.status_code == status.HTTP_200_OK
    return response.json()["access_token"]


def _create_owner_admin() -> dict:
    username = unique_name("bot_key_owner")
    password = strong_password("BotKeyOwner")

    async def _create() -> int:
        async with TestSession() as session:
            db_admin = Admin(username=username, hashed_password=await hash_password(password), role_id=1)
            session.add(db_admin)
            await session.commit()
            await session.refresh(db_admin)
            return db_admin.id

    return {"id": asyncio.run(_create()), "username": username, "password": password}


def _delete_admin_row(username: str) -> None:
    async def _delete() -> None:
        async with TestSession() as session:
            db_admin = (await session.execute(select(Admin).where(Admin.username == username))).scalar_one_or_none()
            if db_admin is not None:
                await session.delete(db_admin)
                await session.commit()

    asyncio.run(_delete())


def _forge(claims: dict) -> str:
    secret = asyncio.run(get_secret_key())
    now = datetime.now(UTC)
    return jwt.encode(
        {"access": "admin", "iat": now, "exp": now + timedelta(minutes=30), **claims}, secret, algorithm="HS256"
    )


def test_a_fresh_login_token_authenticates(access_token):
    admin = create_admin(access_token, role_id=2)
    try:
        token = _login(admin["username"], admin["password"])
        response = client.get("/api/admin", headers=auth_headers(token))
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["username"] == admin["username"]
    finally:
        delete_admin(access_token, admin["username"])


def test_a_token_of_a_deleted_admin_does_not_authenticate_its_replacement(access_token):
    admin = create_admin(access_token, role_id=2)
    old_token = _login(admin["username"], admin["password"])
    delete_admin(access_token, admin["username"])

    replacement = create_admin(access_token, username=admin["username"], role_id=2)
    try:
        response = client.get("/api/admin", headers=auth_headers(old_token))
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        fresh = _login(replacement["username"], replacement["password"])
        assert client.get("/api/admin", headers=auth_headers(fresh)).status_code == status.HTTP_200_OK
    finally:
        delete_admin(access_token, replacement["username"])


def test_a_token_whose_password_hash_changed_out_of_band_is_rejected(access_token):
    admin = create_admin(access_token, role_id=2)
    try:
        token = _login(admin["username"], admin["password"])

        async def _rehash() -> None:
            async with TestSession() as session:
                db_admin = await get_admin_by_username(session, admin["username"], load_users=False)
                db_admin.hashed_password = await hash_password(strong_password("Rotated"))
                await session.commit()

        asyncio.run(_rehash())
        response = client.get("/api/admin", headers=auth_headers(token))
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
    finally:
        delete_admin(access_token, admin["username"])


def test_a_database_admin_id_never_falls_back_to_an_env_sudoer():
    token = _forge({"sub": "testadmin", "aid": 987654321})
    response = client.get("/api/admin", headers=auth_headers(token))
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_an_unbound_token_for_a_database_admin_is_rejected(access_token):
    admin = create_admin(access_token, role_id=2)
    try:
        token = _forge({"sub": admin["username"], "aid": admin["id"]})
        response = client.get("/api/admin", headers=auth_headers(token))
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
    finally:
        delete_admin(access_token, admin["username"])


def test_an_env_sudoer_token_still_authenticates(access_token):
    response = client.get("/api/admin", headers=auth_headers(access_token))
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["username"] == "testadmin"


def _own_connection_sessions():
    connect_args = {"timeout": 30} if IS_SQLITE else {}
    engine = create_async_engine(DATABASE_URL, poolclass=NullPool, connect_args=connect_args)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.skipif(":memory:" in DATABASE_URL, reason="needs a database shared by several connections")
@pytest.mark.parametrize("authenticate", [get_admin, get_admin_with_metrics], ids=["plain", "with_metrics"])
def test_old_token_never_authenticates_as_the_replacement_while_it_is_replaced(access_token, authenticate):
    admin = create_admin(access_token, role_id=2)
    old_token = _login(admin["username"], admin["password"])
    username = admin["username"]

    async def _scenario() -> tuple[list, int]:
        engine, sessions = _own_connection_sessions()
        replaced = asyncio.Event()
        writer_done = asyncio.Event()
        new_hash = await hash_password(strong_password("Replaced"))

        async def _reader() -> list:
            seen = []
            reads_after_replacement = 0
            for _ in range(5000):
                if reads_after_replacement >= 5 or (writer_done.is_set() and not replaced.is_set()):
                    break
                was_replaced = replaced.is_set()
                async with sessions() as db:
                    result = await authenticate(db, old_token)
                    await db.rollback()
                seen.append((was_replaced, result.id if result is not None else None))
                reads_after_replacement += was_replaced
                await asyncio.sleep(0.001)
            return seen

        async def _replace() -> int:
            try:
                await asyncio.sleep(0.005)
                async with sessions() as db:
                    db_admin = await get_admin_by_username(db, username, load_users=False)
                    await db.delete(db_admin)
                    await db.commit()
                await asyncio.sleep(0.005)
                async with sessions() as db:
                    new_admin = Admin(username=username, hashed_password=new_hash, role_id=2)
                    db.add(new_admin)
                    await db.commit()
                    new_id = new_admin.id
                replaced.set()
                return new_id
            finally:
                writer_done.set()

        try:
            results = await asyncio.gather(_reader(), _reader(), _reader(), _replace())
        finally:
            await engine.dispose()
        return [entry for reader in results[:3] for entry in reader], results[3]

    try:
        seen, new_id = asyncio.run(_scenario())
        after = [admin_id for was_replaced, admin_id in seen if was_replaced]
        assert after
        assert all(admin_id is None for admin_id in after)
        assert all(admin_id in (None, admin["id"]) for was_replaced, admin_id in seen if not was_replaced)
        assert new_id is not None
        response = client.get("/api/admin", headers=auth_headers(old_token))
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
    finally:
        _delete_admin_row(username)


def test_sales_bot_api_key_calls_are_unchanged_by_token_binding():
    owner = _create_owner_admin()
    owner_token = _login(owner["username"], owner["password"])
    try:
        created = client.post(
            "/api/api_key",
            headers=auth_headers(owner_token),
            json={"name": unique_name("bot"), "inherit_permissions": True},
        )
        assert created.status_code == status.HTTP_201_CREATED
        key = {"X-API-Key": created.json()["api_key"]}

        username = unique_name("bot_user")
        expire = int((datetime.now(UTC) + timedelta(days=30)).timestamp())
        response = client.post(
            "/api/user",
            headers=key,
            json={
                "username": username,
                "proxy_settings": {},
                "data_limit": 1024**3,
                "expire": expire,
                "data_limit_reset_strategy": "no_reset",
                "status": "active",
                "note": "1047662735 | bot | buy",
            },
        )
        assert response.status_code == status.HTTP_201_CREATED, response.text
        assert client.get(f"/api/user/{username}", headers=key).status_code == status.HTTP_200_OK
        response = client.put(
            f"/api/user/{username}", headers=key, json={"data_limit": 2 * 1024**3, "expire": expire + 86400}
        )
        assert response.status_code == status.HTTP_200_OK, response.text
        assert response.json()["data_limit"] == 2 * 1024**3
        assert client.post(f"/api/user/{username}/reset", headers=key).status_code == status.HTTP_200_OK
        assert client.post(f"/api/user/{username}/revoke_sub", headers=key).status_code == status.HTTP_200_OK
        response = client.put(f"/api/user/{username}", headers=key, json={"status": "disabled"})
        assert response.status_code == status.HTTP_200_OK, response.text
        response = client.get("/api/users", headers=key, params={"limit": 1})
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["total"] >= 1
        assert client.get("/api/groups", headers=key).status_code == status.HTTP_200_OK
        assert client.delete(f"/api/user/{username}", headers=key).status_code == status.HTTP_204_NO_CONTENT
    finally:
        _delete_admin_row(owner["username"])
