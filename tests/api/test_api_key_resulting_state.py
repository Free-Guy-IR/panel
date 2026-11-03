import pytest
from fastapi import status

from tests.api import client
from tests.api.helpers import auth_headers, create_admin, create_user, delete_admin, unique_name

SCOPE_OWN = {"scope": 1}
SCOPE_ALL = {"scope": 2}


def _login(username: str, password: str) -> str:
    response = client.post(
        "/api/admin/token",
        data={"username": username, "password": password, "grant_type": "password"},
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    return response.json()["access_token"]


@pytest.fixture
def operator(access_token):
    role = client.post(
        "/api/admin-role",
        headers=auth_headers(access_token),
        json={
            "name": unique_name("key_state_role"),
            "permissions": {
                "users": {"read": SCOPE_OWN, "read_simple": SCOPE_OWN},
                "api_keys": {"create": True, "read": True, "update": True},
            },
            "limits": {},
            "features": {},
            "access": {},
        },
    )
    assert role.status_code == status.HTTP_201_CREATED, role.text
    role_id = role.json()["id"]
    actor = create_admin(access_token, role_id=role_id)
    victim = create_user(access_token)
    try:
        yield {"token": _login(actor["username"], actor["password"]), "victim": victim}
    finally:
        client.delete(f"/api/user/by-id/{victim['id']}", headers=auth_headers(access_token))
        delete_admin(access_token, actor["username"])
        client.delete(f"/api/admin-role/{role_id}", headers=auth_headers(access_token))


def _create_key(token: str, **payload) -> tuple[int, str]:
    response = client.post(
        "/api/api_key", headers=auth_headers(token), json={"name": unique_name("state_key"), **payload}
    )
    assert response.status_code == status.HTTP_201_CREATED, response.text
    return response.json()["id"], response.json()["api_key"]


def _victim_visible(raw_key: str, victim: dict) -> bool:
    response = client.get("/api/users", headers={"X-Api-Key": raw_key}, params={"username": victim["username"]})
    if response.status_code == status.HTTP_403_FORBIDDEN:
        return False
    assert response.status_code == status.HTTP_200_OK, response.text
    return victim["username"] in {user["username"] for user in response.json()["users"]}


def test_two_step_patch_cannot_widen_an_inheriting_key(operator):
    headers = auth_headers(operator["token"])
    key_id, raw_key = _create_key(operator["token"])

    client.patch(
        f"/api/api_key/{key_id}",
        headers=headers,
        json={"permissions": {"users": {"read": SCOPE_ALL, "read_simple": SCOPE_ALL}, "admins": {"read": True}}},
    )
    client.patch(f"/api/api_key/{key_id}", headers=headers, json={"inherit_permissions": False})

    assert not _victim_visible(raw_key, operator["victim"])
    assert client.get("/api/admins", headers={"X-Api-Key": raw_key}).status_code == status.HTTP_403_FORBIDDEN


def test_an_inheriting_key_never_stores_a_permission_snapshot(operator):
    headers = auth_headers(operator["token"])
    key_id, _ = _create_key(operator["token"])

    client.patch(f"/api/api_key/{key_id}", headers=headers, json={"permissions": {"users": {"read": SCOPE_ALL}}})

    stored = client.get(f"/api/api_key/{key_id}", headers=headers).json()["permissions"]
    assert all(value is None for value in stored.values()), stored


@pytest.mark.parametrize("scoped", [{"scope": 99}, {"scope": 1, "bogus": 2}])
def test_patch_rejects_malformed_scopes_on_a_custom_key(operator, scoped):
    headers = auth_headers(operator["token"])
    key_id, raw_key = _create_key(
        operator["token"], inherit_permissions=False, permissions={"users": {"read": SCOPE_OWN}}
    )

    response = client.patch(
        f"/api/api_key/{key_id}", headers=headers, json={"permissions": {"users": {"read": scoped}}}
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN, response.text
    assert client.get("/api/admin", headers={"X-Api-Key": raw_key}).status_code == status.HTTP_200_OK


@pytest.mark.parametrize("scoped", [{"scope": 99}, {"scope": 1, "bogus": 2}])
def test_malformed_scopes_sent_while_inheriting_never_activate(operator, scoped):
    headers = auth_headers(operator["token"])
    key_id, raw_key = _create_key(operator["token"])

    client.patch(f"/api/api_key/{key_id}", headers=headers, json={"permissions": {"users": {"read": scoped}}})
    client.patch(f"/api/api_key/{key_id}", headers=headers, json={"inherit_permissions": False})

    response = client.get("/api/users", headers={"X-Api-Key": raw_key})
    assert response.status_code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN), response.text


def test_patch_with_null_permissions_keeps_a_custom_key_working(operator):
    headers = auth_headers(operator["token"])
    key_id, raw_key = _create_key(
        operator["token"], inherit_permissions=False, permissions={"users": {"read": SCOPE_OWN}}
    )

    renamed = client.patch(
        f"/api/api_key/{key_id}", headers=headers, json={"name": unique_name("renamed"), "permissions": None}
    )
    assert renamed.status_code == status.HTTP_200_OK, renamed.text

    assert client.get("/api/admin", headers={"X-Api-Key": raw_key}).status_code == status.HTTP_200_OK
    assert client.get("/api/users", headers={"X-Api-Key": raw_key}).status_code == status.HTTP_200_OK


def test_switching_to_custom_with_in_scope_permissions_still_works(operator):
    headers = auth_headers(operator["token"])
    key_id, raw_key = _create_key(operator["token"])

    response = client.patch(
        f"/api/api_key/{key_id}",
        headers=headers,
        json={"inherit_permissions": False, "permissions": {"users": {"read": SCOPE_OWN}}},
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    assert client.get("/api/users", headers={"X-Api-Key": raw_key}).status_code == status.HTTP_200_OK
    assert not _victim_visible(raw_key, operator["victim"])
