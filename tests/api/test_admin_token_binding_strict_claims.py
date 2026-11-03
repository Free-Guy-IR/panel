import asyncio
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi import status
from sqlalchemy import select

from app.db.crud.admin import get_admin as get_admin_by_username
from app.db.models import Admin
from app.models.admin import hash_password
from app.utils import jwt as jwt_utils
from app.utils.jwt import get_admin_token_binding, get_secret_key
from tests.api import TestSession, client
from tests.api.helpers import auth_headers, create_admin, delete_admin, strong_password, unique_name


def _stored_hash(username: str) -> str:
    async def _load() -> str:
        async with TestSession() as session:
            return (await get_admin_by_username(session, username, load_users=False)).hashed_password

    return asyncio.run(_load())


def _bound_token(admin_id: int, username: str, hashed_password: str, *, iat: bool = True, exp: bool = True) -> str:
    secret = asyncio.run(get_secret_key())
    now = datetime.now(UTC)
    claims = {
        "sub": username,
        "access": "admin",
        "aid": admin_id,
        "binding": get_admin_token_binding(hashed_password, secret),
    }
    if iat:
        claims["iat"] = now
    if exp:
        claims["exp"] = now + timedelta(minutes=30)
    return jwt.encode(claims, secret, algorithm="HS256")


def _status(token: str) -> int:
    return client.get("/api/admin", headers=auth_headers(token)).status_code


@pytest.fixture
def expiring_tokens(monkeypatch):
    monkeypatch.setattr(jwt_utils.jwt_settings, "access_token_expire_minutes", 60)


@pytest.fixture
def bound_admin(access_token):
    admin = create_admin(access_token, role_id=2)
    admin["hash"] = _stored_hash(admin["username"])
    yield admin
    delete_admin(access_token, admin["username"])


def _insert_owner() -> dict:
    username = unique_name("strict_owner")
    password = strong_password("StrictOwner")

    async def _insert() -> int:
        async with TestSession() as session:
            db_admin = Admin(username=username, hashed_password=await hash_password(password), role_id=1)
            session.add(db_admin)
            await session.commit()
            await session.refresh(db_admin)
            return db_admin.id

    return {"id": asyncio.run(_insert()), "username": username, "password": password}


def _drop(username: str) -> None:
    async def _delete() -> None:
        async with TestSession() as session:
            db_admin = (await session.execute(select(Admin).where(Admin.username == username))).scalar_one_or_none()
            if db_admin is not None:
                await session.delete(db_admin)
                await session.commit()

    asyncio.run(_delete())


def _login_token(username: str, password: str) -> str:
    response = client.post(
        "/api/admin/token", data={"username": username, "password": password, "grant_type": "password"}
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    return response.json()["access_token"]


def test_a_correctly_bound_token_with_iat_and_exp_authenticates(expiring_tokens, bound_admin):
    token = _bound_token(bound_admin["id"], bound_admin["username"], bound_admin["hash"])
    assert _status(token) == status.HTTP_200_OK


def test_a_correctly_bound_token_without_exp_is_refused_while_tokens_expire(expiring_tokens, bound_admin):
    token = _bound_token(bound_admin["id"], bound_admin["username"], bound_admin["hash"], exp=False)
    assert _status(token) == status.HTTP_401_UNAUTHORIZED


def test_a_correctly_bound_token_without_iat_is_refused(expiring_tokens, bound_admin):
    token = _bound_token(bound_admin["id"], bound_admin["username"], bound_admin["hash"], iat=False)
    assert _status(token) == status.HTTP_401_UNAUTHORIZED


def test_a_bound_token_cannot_borrow_another_admins_id_or_name(access_token, expiring_tokens, bound_admin):
    owner = _insert_owner()
    try:
        for admin_id, username in (
            (owner["id"], owner["username"]),
            (owner["id"], bound_admin["username"]),
            (bound_admin["id"], owner["username"]),
        ):
            assert _status(_bound_token(admin_id, username, bound_admin["hash"])) == status.HTTP_401_UNAUTHORIZED
    finally:
        _drop(owner["username"])


def test_a_non_owner_session_still_cannot_change_its_own_role_or_status(bound_admin):
    headers = auth_headers(_login_token(bound_admin["username"], bound_admin["password"]))
    role = client.put(f"/api/admin/by-id/{bound_admin['id']}", headers=headers, json={"role_id": 3})
    assert role.status_code == status.HTTP_403_FORBIDDEN, role.text
    assert role.json()["detail"] == "You're not allowed to change your own role."
    disabled = client.put(f"/api/admin/by-id/{bound_admin['id']}", headers=headers, json={"status": "disabled"})
    assert disabled.status_code == status.HTTP_403_FORBIDDEN, disabled.text
    assert disabled.json()["detail"] == "You're not allowed to disable your own account."


def test_an_owner_api_key_authenticates_only_as_an_api_key():
    owner = _insert_owner()
    try:
        session = auth_headers(_login_token(owner["username"], owner["password"]))
        created = client.post(
            "/api/api_key", headers=session, json={"name": unique_name("strict_key"), "inherit_permissions": True}
        )
        assert created.status_code == status.HTTP_201_CREATED, created.text
        raw_key = created.json()["api_key"]
        assert client.get("/api/users", headers={"X-Api-Key": raw_key}).status_code == status.HTTP_200_OK
        assert client.get("/api/users", headers=auth_headers(raw_key)).status_code == status.HTTP_401_UNAUTHORIZED
        refused = client.put(
            f"/api/admin/by-id/{owner['id']}",
            headers={"X-Api-Key": raw_key},
            json={"password": strong_password("StolenKey")},
        )
        assert refused.status_code == status.HTTP_403_FORBIDDEN, refused.text
        assert client.get("/api/users", headers={"X-Api-Key": raw_key}).status_code == status.HTTP_200_OK
    finally:
        _drop(owner["username"])
