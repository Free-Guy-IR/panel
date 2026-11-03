from datetime import UTC, datetime, timedelta

from fastapi import status

from tests.api import client
from tests.api.helpers import auth_headers, create_admin, create_user, delete_admin, delete_user, unique_name

MB = 1024 * 1024
DAY = 24 * 60 * 60


def _role(access_token: str, *, require_template: bool = False) -> dict:
    response = client.post(
        "/api/admin-role",
        headers=auth_headers(access_token),
        json={
            "name": unique_name("bulk_bounded_role"),
            "permissions": {"users": {"create": True, "read": True, "update": True, "delete": True}},
            "limits": {
                "max_users": None,
                "data_limit_min": 2 * MB,
                "data_limit_max": 10 * MB,
                "expire_min": 60 * 60,
                "expire_max": DAY,
                "min_hwid_per_user": None,
                "max_hwid_per_user": None,
            },
            "features": {"can_use_reset_strategy": True, "can_use_next_plan": True},
            "access": {"require_template": require_template, "allowed_template_ids": None, "allowed_group_ids": None},
        },
    )
    assert response.status_code == status.HTTP_201_CREATED, response.text
    return response.json()


def _delete_role(access_token: str, role_id: int) -> None:
    client.delete(f"/api/admin-role/{role_id}", headers=auth_headers(access_token))


def _login(username: str, password: str) -> str:
    response = client.post(
        "/api/admin/token", data={"username": username, "password": password, "grant_type": "password"}
    )
    assert response.status_code == status.HTTP_200_OK
    return response.json()["access_token"]


def _bounded_user(access_token: str) -> dict:
    expire = (datetime.now(UTC).replace(microsecond=0) + timedelta(hours=2)).isoformat()
    return create_user(
        access_token,
        payload={"username": unique_name("bulk_bounded_user"), "data_limit": 5 * MB, "expire": expire},
    )


def _fetch(access_token: str, username: str) -> dict:
    response = client.get(f"/api/user/{username}", headers=auth_headers(access_token))
    assert response.status_code == status.HTTP_200_OK
    return response.json()


def test_limited_admin_bulk_data_limit_beyond_the_role_maximum_is_rejected_and_writes_nothing(access_token):
    role = _role(access_token)
    admin = create_admin(access_token, role_id=role["id"])
    token = _login(admin["username"], admin["password"])
    user = _bounded_user(access_token)
    try:
        for dry_run in (True, False):
            response = client.post(
                "/api/users/bulk/data_limit",
                headers=auth_headers(token),
                json={"amount": 100 * MB, "users": [user["id"]], "dry_run": dry_run},
            )
            assert response.status_code == status.HTTP_400_BAD_REQUEST, response.text
            assert "Data limit cannot exceed" in response.json()["detail"]
        response = client.post(
            "/api/users/bulk/data_limit",
            headers=auth_headers(token),
            json={"amount": -4 * MB, "users": [user["id"]]},
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.text
        assert "Data limit must be at least" in response.json()["detail"]
        assert _fetch(access_token, user["username"])["data_limit"] == 5 * MB

        response = client.post(
            "/api/users/bulk/data_limit",
            headers=auth_headers(token),
            json={"amount": 1 * MB, "users": [user["id"]]},
        )
        assert response.status_code == status.HTTP_200_OK, response.text
        assert _fetch(access_token, user["username"])["data_limit"] == 6 * MB
    finally:
        delete_user(access_token, user["username"])
        delete_admin(access_token, admin["username"])
        _delete_role(access_token, role["id"])


def test_limited_admin_bulk_expire_beyond_the_role_maximum_is_rejected_and_writes_nothing(access_token):
    role = _role(access_token)
    admin = create_admin(access_token, role_id=role["id"])
    token = _login(admin["username"], admin["password"])
    user = _bounded_user(access_token)
    try:
        before = _fetch(access_token, user["username"])["expire"]
        for dry_run in (True, False):
            response = client.post(
                "/api/users/bulk/expire",
                headers=auth_headers(token),
                json={"amount": 30 * DAY, "users": [user["id"]], "dry_run": dry_run},
            )
            assert response.status_code == status.HTTP_400_BAD_REQUEST, response.text
            assert "Expire cannot exceed" in response.json()["detail"]
        response = client.post(
            "/api/users/bulk/expire",
            headers=auth_headers(token),
            json={"amount": -2 * 60 * 60 + 60, "users": [user["id"]]},
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.text
        assert "Expire must be at least" in response.json()["detail"]
        assert _fetch(access_token, user["username"])["expire"] == before

        response = client.post(
            "/api/users/bulk/expire",
            headers=auth_headers(token),
            json={"amount": 60 * 60, "users": [user["id"]]},
        )
        assert response.status_code == status.HTTP_200_OK, response.text
        assert _fetch(access_token, user["username"])["expire"] != before
    finally:
        delete_user(access_token, user["username"])
        delete_admin(access_token, admin["username"])
        _delete_role(access_token, role["id"])


def test_template_only_admin_cannot_bulk_change_quota_or_expiry(access_token):
    role = _role(access_token, require_template=True)
    admin = create_admin(access_token, role_id=role["id"])
    token = _login(admin["username"], admin["password"])
    user = _bounded_user(access_token)
    try:
        for path, amount in (("data_limit", 1 * MB), ("expire", 60 * 60)):
            response = client.post(
                f"/api/users/bulk/{path}", headers=auth_headers(token), json={"amount": amount, "users": [user["id"]]}
            )
            assert response.status_code == status.HTTP_403_FORBIDDEN, (path, response.text)
        after = _fetch(access_token, user["username"])
        assert after["data_limit"] == 5 * MB
        assert after["expire"] == user["expire"]
    finally:
        delete_user(access_token, user["username"])
        delete_admin(access_token, admin["username"])
        _delete_role(access_token, role["id"])


def test_owner_bulk_quota_and_expiry_are_not_bounded_by_any_role(access_token):
    user = _bounded_user(access_token)
    try:
        response = client.post(
            "/api/users/bulk/data_limit",
            headers=auth_headers(access_token),
            json={"amount": 100 * MB, "users": [user["id"]]},
        )
        assert response.status_code == status.HTTP_200_OK, response.text
        response = client.post(
            "/api/users/bulk/expire",
            headers=auth_headers(access_token),
            json={"amount": 30 * DAY, "users": [user["id"]]},
        )
        assert response.status_code == status.HTTP_200_OK, response.text
        after = _fetch(access_token, user["username"])
        assert after["data_limit"] == 105 * MB
        assert after["expire"] != user["expire"]
    finally:
        delete_user(access_token, user["username"])
