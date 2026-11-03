from datetime import UTC, datetime, timedelta

import pytest
from fastapi import status
from sqlalchemy import update

import app.db.crud.bulk as bulk_crud
from app.db.models import User
from tests.api import IS_SQLITE, TestSession, client
from tests.api.helpers import auth_headers, create_admin, create_user, delete_admin, delete_user, unique_name

MB = 1024 * 1024
DAY = 24 * 60 * 60
EXPIRE_STEP = 10**10
DATA_STEP = 2**62
BIGINT_MAX = 2**63 - 1
FAR_FUTURE = datetime(9800, 1, 1, tzinfo=UTC)

shared_sqlite_connection_only = pytest.mark.skipif(
    not IS_SQLITE, reason="on a server database the injected writer waits for the request's own row lock"
)


def _role(access_token: str, limits: dict) -> dict:
    response = client.post(
        "/api/admin-role",
        headers=auth_headers(access_token),
        json={
            "name": unique_name("bulk_window_role"),
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


def _limited_token(access_token: str, limits: dict) -> tuple[dict, dict, str]:
    role = _role(access_token, limits)
    admin = create_admin(access_token, role_id=role["id"])
    response = client.post(
        "/api/admin/token",
        data={"username": admin["username"], "password": admin["password"], "grant_type": "password"},
    )
    assert response.status_code == status.HTTP_200_OK
    return role, admin, response.json()["access_token"]


def _drop_limited(access_token: str, role: dict, admin: dict) -> None:
    delete_admin(access_token, admin["username"])
    client.delete(f"/api/admin-role/{role['id']}", headers=auth_headers(access_token))


def _user(access_token: str) -> dict:
    expire = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=2)
    return create_user(
        access_token,
        payload={"username": unique_name("bulk_window_user"), "data_limit": 5 * MB, "expire": expire.isoformat()},
    )


def _write_once_before(monkeypatch, name: str, user_id: int, **values) -> list[bool]:
    original = getattr(bulk_crud, name)
    fired: list[bool] = []

    async def wrapped(*args, **kwargs):
        if not fired:
            fired.append(True)
            async with TestSession() as other:
                await other.execute(update(User).where(User.id == user_id).values(**values))
                await other.commit()
        return await original(*args, **kwargs)

    monkeypatch.setattr(bulk_crud, name, wrapped)
    return fired


def _post(token: str, field: str, amount: int, user_ids: list[int]):
    return client.post(
        f"/api/users/bulk/{field}", headers=auth_headers(token), json={"amount": amount, "users": user_ids}
    )


def _fetch(access_token: str, username: str) -> dict:
    response = client.get(f"/api/user/{username}", headers=auth_headers(access_token))
    assert response.status_code == status.HTTP_200_OK, response.text
    return response.json()


def _assert_kept(access_token: str, username: str, field: str) -> None:
    stored = _fetch(access_token, username)[field]
    if field == "expire":
        assert stored is not None and stored.startswith("9800-01-01"), stored
    else:
        assert stored == DATA_STEP, stored


@shared_sqlite_connection_only
@pytest.mark.parametrize(
    "field,amount,values",
    [("expire", EXPIRE_STEP, {"expire": FAR_FUTURE}), ("data_limit", DATA_STEP, {"data_limit": DATA_STEP})],
)
def test_owner_value_moved_after_the_locked_check_is_rejected_and_kept(
    access_token, monkeypatch, field, amount, values
):
    near = _user(access_token)
    other = _user(access_token)
    try:
        fired = _write_once_before(monkeypatch, "_apply_to_locked_targets", other["id"], **values)
        response = _post(access_token, field, amount, [near["id"], other["id"]])
        monkeypatch.undo()
        assert fired == [True]
        _assert_kept(access_token, other["username"], field)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, response.text
    finally:
        monkeypatch.undo()
        delete_user(access_token, near["username"])
        delete_user(access_token, other["username"])


@shared_sqlite_connection_only
@pytest.mark.parametrize(
    "limits,field,amount,values",
    [
        ({"expire_max": 400 * 365 * DAY}, "expire", EXPIRE_STEP, {"expire": FAR_FUTURE}),
        ({"expire_min": 60}, "expire", EXPIRE_STEP, {"expire": FAR_FUTURE}),
        ({"data_limit_max": 2**63 - 2}, "data_limit", DATA_STEP, {"data_limit": DATA_STEP}),
        ({"data_limit_min": MB}, "data_limit", DATA_STEP, {"data_limit": DATA_STEP}),
    ],
)
def test_limited_admin_value_moved_after_the_locked_check_is_rejected_and_kept(
    access_token, monkeypatch, limits, field, amount, values
):
    role, admin, token = _limited_token(access_token, limits)
    near = _user(access_token)
    other = _user(access_token)
    try:
        fired = _write_once_before(monkeypatch, "_apply_to_locked_targets", other["id"], **values)
        response = _post(token, field, amount, [near["id"], other["id"]])
        monkeypatch.undo()
        assert fired == [True]
        _assert_kept(access_token, other["username"], field)
        assert response.status_code in (status.HTTP_409_CONFLICT, status.HTTP_422_UNPROCESSABLE_ENTITY), response.text
    finally:
        monkeypatch.undo()
        delete_user(access_token, near["username"])
        delete_user(access_token, other["username"])
        _drop_limited(access_token, role, admin)


@pytest.mark.parametrize(
    "field,amount,values",
    [("expire", EXPIRE_STEP, {"expire": FAR_FUTURE}), ("data_limit", DATA_STEP, {"data_limit": DATA_STEP})],
)
def test_owner_value_moved_between_the_pre_check_and_the_lock_is_rejected_and_kept(
    access_token, monkeypatch, field, amount, values
):
    near = _user(access_token)
    other = _user(access_token)
    try:
        fired = _write_once_before(monkeypatch, "_lock_bulk_targets", other["id"], **values)
        response = _post(access_token, field, amount, [near["id"], other["id"]])
        monkeypatch.undo()
        assert fired == [True]
        _assert_kept(access_token, other["username"], field)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, response.text
    finally:
        monkeypatch.undo()
        delete_user(access_token, near["username"])
        delete_user(access_token, other["username"])
