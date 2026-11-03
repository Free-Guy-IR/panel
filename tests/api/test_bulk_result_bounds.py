from datetime import UTC, datetime, timedelta

import pytest
from fastapi import status

from app.fork.crud.bulk_result_bounds import STORABLE_EXPIRE_MIN
from tests.api import client
from tests.api.helpers import auth_headers, create_admin, create_user, delete_admin, delete_user, unique_name

MB = 1024 * 1024
DAY = 24 * 60 * 60
EXPIRE_AMOUNT_BOUND = 10**10
DATA_LIMIT_AMOUNT_BOUND = 2**62
BIGINT_MAX = 2**63 - 1
FAR_FUTURE = datetime(9800, 1, 1, tzinfo=UTC)
FAR_PAST = datetime(1200, 1, 1, tzinfo=UTC)


def _role(access_token: str, limits: dict) -> dict:
    response = client.post(
        "/api/admin-role",
        headers=auth_headers(access_token),
        json={
            "name": unique_name("bulk_result_role"),
            "permissions": {"users": {"create": True, "read": True, "update": True, "delete": True}},
            "limits": {
                "max_users": None,
                "data_limit_min": None,
                "data_limit_max": None,
                "expire_min": None,
                "expire_max": None,
                "min_hwid_per_user": None,
                "max_hwid_per_user": None,
                **limits,
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


def _user(access_token: str, expire: datetime | None = None, data_limit: int = 5 * MB) -> dict:
    expire = expire or (datetime.now(UTC).replace(microsecond=0) + timedelta(hours=2))
    return create_user(
        access_token,
        payload={"username": unique_name("bulk_result_user"), "data_limit": data_limit, "expire": expire.isoformat()},
    )


def _fetch(access_token: str, username: str) -> dict:
    response = client.get(f"/api/user/{username}", headers=auth_headers(access_token))
    assert response.status_code == status.HTTP_200_OK, response.text
    return response.json()


def _post(token: str, field: str, amount: int, user_id: int, dry_run: bool = False):
    return client.post(
        f"/api/users/bulk/{field}",
        headers=auth_headers(token),
        json={"amount": amount, "users": [user_id], "dry_run": dry_run},
    )


def _limited_token(access_token: str, limits: dict) -> tuple[dict, dict, str]:
    role = _role(access_token, limits)
    admin = create_admin(access_token, role_id=role["id"])
    return role, admin, _login(admin["username"], admin["password"])


def _drop_limited(access_token: str, role: dict, admin: dict) -> None:
    delete_admin(access_token, admin["username"])
    client.delete(f"/api/admin-role/{role['id']}", headers=auth_headers(access_token))


def test_two_in_bound_data_limit_steps_cannot_store_past_bigint(access_token):
    user = _user(access_token)
    try:
        first = _post(access_token, "data_limit", DATA_LIMIT_AMOUNT_BOUND, user["id"])
        assert first.status_code == status.HTTP_200_OK, first.text
        second = _post(access_token, "data_limit", DATA_LIMIT_AMOUNT_BOUND, user["id"])
        stored = _fetch(access_token, user["username"])["data_limit"]
        assert stored == 5 * MB + DATA_LIMIT_AMOUNT_BOUND
        assert stored <= BIGINT_MAX
        assert second.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, second.text
        dry = _post(access_token, "data_limit", DATA_LIMIT_AMOUNT_BOUND, user["id"], True)
        assert dry.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, dry.text
    finally:
        delete_user(access_token, user["username"])


def test_data_limit_step_that_lands_exactly_on_bigint_max_is_accepted(access_token):
    user = _user(access_token, data_limit=BIGINT_MAX - DATA_LIMIT_AMOUNT_BOUND)
    try:
        response = _post(access_token, "data_limit", DATA_LIMIT_AMOUNT_BOUND, user["id"])
        assert response.status_code == status.HTTP_200_OK, response.text
        assert _fetch(access_token, user["username"])["data_limit"] == BIGINT_MAX
        rejected = _post(access_token, "data_limit", 1, user["id"])
        assert rejected.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, rejected.text
        assert _fetch(access_token, user["username"])["data_limit"] == BIGINT_MAX
    finally:
        delete_user(access_token, user["username"])


def test_in_bound_expire_step_on_a_far_future_expire_is_422_and_keeps_expire(access_token):
    user = _user(access_token, expire=FAR_FUTURE)
    try:
        before = _fetch(access_token, user["username"])
        response = _post(access_token, "expire", EXPIRE_AMOUNT_BOUND, user["id"])
        after = _fetch(access_token, user["username"])
        assert after["expire"] is not None, "a finite expire became never-expires"
        assert after["expire"] == before["expire"]
        assert after["status"] == before["status"]
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, response.text
        dry = _post(access_token, "expire", EXPIRE_AMOUNT_BOUND, user["id"], True)
        assert dry.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, dry.text
    finally:
        delete_user(access_token, user["username"])


def test_in_bound_negative_expire_step_on_a_far_past_expire_is_422_and_keeps_expire(access_token):
    user = _user(access_token, expire=FAR_PAST)
    try:
        before = _fetch(access_token, user["username"])
        response = _post(access_token, "expire", -EXPIRE_AMOUNT_BOUND, user["id"])
        assert _fetch(access_token, user["username"])["expire"] == before["expire"]
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, response.text
        dry = _post(access_token, "expire", -EXPIRE_AMOUNT_BOUND, user["id"], True)
        assert dry.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, dry.text
    finally:
        delete_user(access_token, user["username"])


def test_out_of_range_target_rejects_the_whole_request_for_every_target(access_token):
    near = _user(access_token)
    far = _user(access_token, expire=FAR_FUTURE)
    try:
        before = _fetch(access_token, near["username"])
        response = client.post(
            "/api/users/bulk/expire",
            headers=auth_headers(access_token),
            json={"amount": EXPIRE_AMOUNT_BOUND, "users": [near["id"], far["id"]]},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, response.text
        assert _fetch(access_token, near["username"])["expire"] == before["expire"]
    finally:
        delete_user(access_token, near["username"])
        delete_user(access_token, far["username"])


@pytest.mark.parametrize(
    "limits",
    [{"expire_min": 60 * 60, "expire_max": DAY, "data_limit_min": 2 * MB, "data_limit_max": 10 * MB}, {}],
)
def test_limited_admin_in_bound_expire_on_a_far_future_expire_is_422_not_500(access_token, limits):
    role, admin, token = _limited_token(access_token, limits)
    user = _user(access_token, expire=FAR_FUTURE)
    try:
        before = _fetch(access_token, user["username"])
        for dry_run in (True, False):
            response = _post(token, "expire", EXPIRE_AMOUNT_BOUND, user["id"], dry_run)
            assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, response.text
        assert _fetch(access_token, user["username"])["expire"] == before["expire"]
    finally:
        delete_user(access_token, user["username"])
        _drop_limited(access_token, role, admin)


@pytest.mark.parametrize("limits", [{"data_limit_min": 2 * MB, "data_limit_max": 10 * MB}, {}])
def test_limited_admin_data_limit_step_past_bigint_is_422(access_token, limits):
    role, admin, token = _limited_token(access_token, limits)
    user = _user(access_token, data_limit=BIGINT_MAX - 10)
    try:
        for dry_run in (True, False):
            response = _post(token, "data_limit", 11, user["id"], dry_run)
            assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, response.text
        assert _fetch(access_token, user["username"])["data_limit"] == BIGINT_MAX - 10
    finally:
        delete_user(access_token, user["username"])
        _drop_limited(access_token, role, admin)


def test_unlimited_and_unmatched_users_do_not_trip_the_result_bound(access_token):
    unlimited = create_user(access_token, payload={"username": unique_name("bulk_result_unl"), "data_limit": 0})
    normal = _user(access_token)
    try:
        response = client.post(
            "/api/users/bulk/data_limit",
            headers=auth_headers(access_token),
            json={"amount": DATA_LIMIT_AMOUNT_BOUND, "users": [unlimited["id"], normal["id"]]},
        )
        assert response.status_code == status.HTTP_200_OK, response.text
        assert _fetch(access_token, normal["username"])["data_limit"] == 5 * MB + DATA_LIMIT_AMOUNT_BOUND
        assert _fetch(access_token, unlimited["username"])["data_limit"] in (0, None)
    finally:
        delete_user(access_token, unlimited["username"])
        delete_user(access_token, normal["username"])


def test_negative_expire_step_landing_exactly_on_the_lower_edge_after_a_bulk_write_is_accepted(access_token):
    user = _user(access_token, expire=datetime(1000, 1, 1, 0, 0, 3, tzinfo=UTC))
    try:
        first = _post(access_token, "expire", -1, user["id"])
        assert first.status_code == status.HTTP_200_OK, first.text
        assert _fetch(access_token, user["username"])["expire"].startswith("1000-01-01T00:00:02")
        dry = _post(access_token, "expire", -2, user["id"], True)
        assert dry.status_code == status.HTTP_200_OK, dry.text
        response = _post(access_token, "expire", -2, user["id"])
        assert response.status_code == status.HTTP_200_OK, response.text
        assert _fetch(access_token, user["username"])["expire"].startswith("1000-01-01T00:00:00")
        rejected = _post(access_token, "expire", -1, user["id"])
        assert rejected.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, rejected.text
    finally:
        delete_user(access_token, user["username"])


def test_expire_step_landing_inside_the_last_second_of_9999_is_accepted(access_token):
    user = _user(access_token, expire=datetime(9999, 12, 31, 23, 59, 58, 400000, tzinfo=UTC))
    try:
        response = _post(access_token, "expire", 1, user["id"])
        assert response.status_code == status.HTTP_200_OK, response.text
        assert _fetch(access_token, user["username"])["expire"].startswith("9999-12-31T23:59:59")
        rejected = _post(access_token, "expire", 1, user["id"])
        assert rejected.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, rejected.text
    finally:
        delete_user(access_token, user["username"])


LOWER_EDGE_FRACTION_SEED = STORABLE_EXPIRE_MIN + timedelta(seconds=2) - timedelta(microseconds=1)


def test_owner_negative_step_never_stores_an_expire_below_the_storable_minimum(access_token):
    user = _user(access_token, expire=LOWER_EDGE_FRACTION_SEED)
    try:
        response = _post(access_token, "expire", -2, user["id"])
        stored = _fetch(access_token, user["username"])["expire"]
        assert stored is not None and stored >= "1000-01-01T00:00:00", (response.status_code, response.text, stored)
    finally:
        delete_user(access_token, user["username"])


def test_owner_and_limited_admin_agree_on_the_lower_edge(access_token):
    role, admin, token = _limited_token(access_token, {"expire_max": EXPIRE_AMOUNT_BOUND})
    user = _user(access_token, expire=LOWER_EDGE_FRACTION_SEED)
    try:
        owner = _post(access_token, "expire", -2, user["id"], True)
        limited = _post(token, "expire", -2, user["id"], True)
        assert owner.status_code == limited.status_code, (owner.text, limited.text)
    finally:
        delete_user(access_token, user["username"])
        _drop_limited(access_token, role, admin)
