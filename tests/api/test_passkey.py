import asyncio

from fastapi import status
from sqlalchemy import select

from app.db.models import Admin, PasskeyChallenge
from tests.api import TestSession, client
from tests.api.helpers import auth_headers, create_admin, delete_admin


def _login(username: str, password: str) -> str:
    response = client.post(
        "/api/admin/token",
        data={"username": username, "password": password, "grant_type": "password"},
    )
    assert response.status_code == status.HTTP_200_OK
    return response.json()["access_token"]


def _admin_id(username: str) -> int:
    async def _load() -> int:
        async with TestSession() as session:
            return (await session.execute(select(Admin.id).where(Admin.username == username))).scalar_one()

    return asyncio.run(_load())


def _login_challenges_without_admin() -> int:
    async def _count() -> int:
        async with TestSession() as session:
            rows = await session.execute(
                select(PasskeyChallenge.id).where(
                    PasskeyChallenge.kind == "login", PasskeyChallenge.admin_id.is_(None)
                )
            )
            return len(rows.scalars().all())

    return asyncio.run(_count())


def test_passkey_routes_are_mounted_and_an_admin_starts_with_none(access_token):
    admin = create_admin(access_token, role_id=3)
    try:
        token = _login(admin["username"], admin["password"])
        response = client.get("/api/admin/passkey", headers=auth_headers(token))
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == []
    finally:
        delete_admin(access_token, admin["username"])


def test_login_options_reject_an_unknown_admin():
    response = client.post("/api/admin/passkey/login/options", json={"username": "no_such_admin_passkey"})
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_login_options_report_an_admin_without_a_passkey(access_token):
    admin = create_admin(access_token, role_id=3)
    try:
        response = client.post("/api/admin/passkey/login/options", json={"username": admin["username"]})
        assert response.status_code == status.HTTP_404_NOT_FOUND
    finally:
        delete_admin(access_token, admin["username"])


def test_usernameless_login_options_store_an_unscoped_challenge():
    before = _login_challenges_without_admin()
    response = client.post("/api/admin/passkey/login/options", json={})
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["challenge"]
    assert not body.get("allowCredentials")
    assert _login_challenges_without_admin() == before + 1


def test_passkey_login_verify_without_a_known_credential_is_rejected():
    response = client.post(
        "/api/admin/passkey/login/verify",
        json={"credential": {"rawId": "AAAA", "response": {"clientDataJSON": "e30"}}},
    )
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_registration_options_describe_the_calling_admin(access_token):
    admin = create_admin(access_token, role_id=3)
    try:
        token = _login(admin["username"], admin["password"])
        response = client.post("/api/admin/passkey/register/options", headers=auth_headers(token))
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["user"]["name"] == admin["username"]
        assert body["rp"]["id"] == "testserver"
        assert body["authenticatorSelection"]["residentKey"] == "required"
    finally:
        delete_admin(access_token, admin["username"])


def test_an_admin_cannot_register_a_passkey_for_another_admin(access_token):
    first = create_admin(access_token, role_id=2)
    second = create_admin(access_token, role_id=3)
    try:
        token = _login(first["username"], first["password"])
        response = client.post(
            f"/api/admin/passkey/admins/{_admin_id(second['username'])}/register/options",
            headers=auth_headers(token),
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN
        response = client.post(
            f"/api/admin/passkey/admins/{_admin_id(first['username'])}/register/options",
            headers=auth_headers(token),
        )
        assert response.status_code == status.HTTP_200_OK
    finally:
        delete_admin(access_token, first["username"])
        delete_admin(access_token, second["username"])


def test_managing_another_admins_passkeys_needs_the_passkeys_permission(access_token):
    operator = create_admin(access_token, role_id=3)
    administrator = create_admin(access_token, role_id=2)
    try:
        operator_id = _admin_id(operator["username"])
        administrator_id = _admin_id(administrator["username"])

        operator_token = _login(operator["username"], operator["password"])
        denied = client.get(f"/api/admin/passkey/admins/{administrator_id}", headers=auth_headers(operator_token))
        assert denied.status_code == status.HTTP_403_FORBIDDEN
        denied_delete = client.delete(
            f"/api/admin/passkey/admins/{administrator_id}/1", headers=auth_headers(operator_token)
        )
        assert denied_delete.status_code == status.HTTP_403_FORBIDDEN
        own = client.get(f"/api/admin/passkey/admins/{operator_id}", headers=auth_headers(operator_token))
        assert own.status_code == status.HTTP_200_OK

        administrator_token = _login(administrator["username"], administrator["password"])
        allowed = client.get(f"/api/admin/passkey/admins/{operator_id}", headers=auth_headers(administrator_token))
        assert allowed.status_code == status.HTTP_200_OK
        assert allowed.json() == []
    finally:
        delete_admin(access_token, operator["username"])
        delete_admin(access_token, administrator["username"])
