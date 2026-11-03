from datetime import UTC, datetime, timedelta

import pytest
from fastapi import status

from tests.api import client
from tests.api.helpers import auth_headers, create_admin, create_user, delete_admin, delete_user, unique_name

MB = 1024 * 1024
DAY = 24 * 60 * 60
EXPIRE_AMOUNT_BOUND = 10**10
DATA_LIMIT_AMOUNT_BOUND = 2**62


def _bounded_role(access_token: str) -> dict:
    response = client.post(
        "/api/admin-role",
        headers=auth_headers(access_token),
        json={
            "name": unique_name("bulk_amount_role"),
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
            "access": {"require_template": False, "allowed_template_ids": None, "allowed_group_ids": None},
        },
    )
    assert response.status_code == status.HTTP_201_CREATED, response.text
    return response.json()


def _login(username: str, password: str) -> str:
    response = client.post(
        "/api/admin/token", data={"username": username, "password": password, "grant_type": "password"}
    )
    assert response.status_code == status.HTTP_200_OK
    return response.json()["access_token"]


def _user(access_token: str) -> dict:
    expire = (datetime.now(UTC).replace(microsecond=0) + timedelta(hours=2)).isoformat()
    return create_user(
        access_token,
        payload={"username": unique_name("bulk_amount_user"), "data_limit": 5 * MB, "expire": expire},
    )


def _fetch(access_token: str, username: str) -> dict:
    response = client.get(f"/api/user/{username}", headers=auth_headers(access_token))
    assert response.status_code == status.HTTP_200_OK
    return response.json()


def _post(token: str, field: str, amount: int, user_id: int, dry_run: bool = False):
    return client.post(
        f"/api/users/bulk/{field}",
        headers=auth_headers(token),
        json={"amount": amount, "users": [user_id], "dry_run": dry_run},
    )


@pytest.mark.parametrize("amount", [10**12, -(10**12), EXPIRE_AMOUNT_BOUND + 1, -EXPIRE_AMOUNT_BOUND - 1])
def test_bulk_expire_amount_outside_the_bound_is_422_for_the_owner_and_keeps_expire(access_token, amount):
    user = _user(access_token)
    try:
        before = _fetch(access_token, user["username"])
        for dry_run in (True, False):
            response = _post(access_token, "expire", amount, user["id"], dry_run)
            assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, response.text
        after = _fetch(access_token, user["username"])
        assert after["expire"] == before["expire"]
        assert after["status"] == before["status"]
    finally:
        delete_user(access_token, user["username"])


def test_owner_bulk_expire_of_ten_to_the_twelve_without_dry_run_keeps_the_stored_expire(access_token):
    user = _user(access_token)
    try:
        before = _fetch(access_token, user["username"])
        response = _post(access_token, "expire", 10**12, user["id"])
        after = _fetch(access_token, user["username"])
        assert after["expire"] is not None, "a finite expire became never-expires"
        assert after["expire"] == before["expire"]
        assert after["status"] == before["status"]
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, response.text
    finally:
        delete_user(access_token, user["username"])


@pytest.mark.parametrize("amount", [2**63, -(2**63), DATA_LIMIT_AMOUNT_BOUND + 1, -DATA_LIMIT_AMOUNT_BOUND - 1])
def test_bulk_data_limit_amount_outside_the_bound_is_422_for_the_owner_and_keeps_data_limit(access_token, amount):
    user = _user(access_token)
    try:
        for dry_run in (True, False):
            response = _post(access_token, "data_limit", amount, user["id"], dry_run)
            assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, response.text
        assert _fetch(access_token, user["username"])["data_limit"] == 5 * MB
    finally:
        delete_user(access_token, user["username"])


@pytest.mark.parametrize(("field", "amount"), [("expire", 10**12), ("data_limit", 2**63)])
def test_limited_admin_bulk_amount_outside_the_bound_is_422_not_a_server_error(access_token, field, amount):
    role = _bounded_role(access_token)
    admin = create_admin(access_token, role_id=role["id"])
    token = _login(admin["username"], admin["password"])
    user = _user(access_token)
    try:
        before = _fetch(access_token, user["username"])
        for dry_run in (True, False):
            response = _post(token, field, amount, user["id"], dry_run)
            assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, response.text
        after = _fetch(access_token, user["username"])
        assert after["expire"] == before["expire"]
        assert after["data_limit"] == before["data_limit"]
    finally:
        delete_user(access_token, user["username"])
        delete_admin(access_token, admin["username"])
        client.delete(f"/api/admin-role/{role['id']}", headers=auth_headers(access_token))


def test_bulk_amounts_at_the_bound_are_accepted_for_the_owner(access_token):
    user = _user(access_token)
    try:
        before = _fetch(access_token, user["username"])
        response = _post(access_token, "expire", EXPIRE_AMOUNT_BOUND, user["id"])
        assert response.status_code == status.HTTP_200_OK, response.text
        moved = datetime.fromisoformat(_fetch(access_token, user["username"])["expire"])
        original = datetime.fromisoformat(before["expire"])
        assert moved - original == timedelta(seconds=EXPIRE_AMOUNT_BOUND)

        response = _post(access_token, "data_limit", DATA_LIMIT_AMOUNT_BOUND, user["id"])
        assert response.status_code == status.HTTP_200_OK, response.text
        assert _fetch(access_token, user["username"])["data_limit"] == 5 * MB + DATA_LIMIT_AMOUNT_BOUND
    finally:
        delete_user(access_token, user["username"])
