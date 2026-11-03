from fastapi import status

from tests.api import client
from tests.api.helpers import auth_headers, create_admin, delete_admin, unique_name


def _login(username: str, password: str) -> str:
    response = client.post(
        "/api/admin/token",
        data={"username": username, "password": password, "grant_type": "password"},
    )
    assert response.status_code == status.HTTP_200_OK
    return response.json()["access_token"]


def _api_key(token: str) -> str:
    response = client.post(
        "/api/api_key",
        headers=auth_headers(token),
        json={"name": unique_name("enroll"), "inherit_permissions": True},
    )
    assert response.status_code == status.HTTP_201_CREATED, response.text
    return response.json()["api_key"]


def test_an_api_key_cannot_start_or_finish_passkey_enrollment(access_token):
    admin = create_admin(access_token, role_id=2)
    try:
        token = _login(admin["username"], admin["password"])
        key = {"X-API-Key": _api_key(token)}
        enrollment = [
            ("/api/admin/passkey/register/options", None),
            ("/api/admin/passkey/register/verify", {"credential": {}}),
            (f"/api/admin/passkey/admins/{admin['id']}/register/options", None),
            (f"/api/admin/passkey/admins/{admin['id']}/register/verify", {"credential": {}}),
        ]
        for url, body in enrollment:
            response = client.post(url, headers=key, json=body) if body is not None else client.post(url, headers=key)
            assert response.status_code == status.HTTP_401_UNAUTHORIZED, (url, response.text)

        session = client.post("/api/admin/passkey/register/options", headers=auth_headers(token))
        assert session.status_code == status.HTTP_200_OK
        assert client.get("/api/admin/passkey", headers=key).status_code == status.HTTP_200_OK
    finally:
        delete_admin(access_token, admin["username"])
