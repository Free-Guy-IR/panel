import asyncio

import pytest
from fastapi import status
from sqlalchemy import update

from app.db.models import User
from tests.api import TestSession, client
from tests.api.concurrent_requests import send_concurrently
from tests.api.helpers import (
    auth_headers,
    create_admin,
    create_user,
    delete_admin,
    set_user_hwids,
    unique_name,
)

SCOPE_OWN = {"scope": 1}
SCOPE_ALL = {"scope": 2}


def _login(username: str, password: str) -> str:
    response = client.post(
        "/api/admin/token", data={"username": username, "password": password, "grant_type": "password"}
    )
    assert response.status_code == status.HTTP_200_OK
    return response.json()["access_token"]


def _role(access_token: str, permissions: dict) -> dict:
    response = client.post(
        "/api/admin-role",
        headers=auth_headers(access_token),
        json={
            "name": unique_name("hwid_count_role"),
            "permissions": permissions,
            "limits": {},
            "features": {},
            "access": {},
        },
    )
    assert response.status_code == status.HTTP_201_CREATED, response.text
    return response.json()


def _hand_user_to(user_id: int, admin_id: int) -> None:
    async def _move() -> None:
        async with TestSession() as session:
            await session.execute(update(User).where(User.id == user_id).values(admin_id=admin_id))
            await session.commit()

    asyncio.run(_move())


@pytest.mark.parametrize(
    ("permissions", "sees_counts"),
    [
        pytest.param({"users": {"read": SCOPE_ALL}}, False, id="without-hwids-read"),
        pytest.param({"users": {"read": SCOPE_ALL}, "hwids": {"read": True}}, True, id="with-hwids-read"),
        pytest.param(
            {"users": {"read": SCOPE_ALL}, "hwids": {"read": SCOPE_OWN}}, False, id="all-users-own-hwids-read"
        ),
    ],
)
def test_user_list_shows_device_counts_only_with_hwids_read(access_token, permissions, sees_counts):
    role = _role(access_token, permissions)
    actor = create_admin(access_token, role_id=role["id"])
    tag = unique_name("hwid_scope")
    first, second = [create_user(access_token, username=f"{tag}_{name}") for name in ("a", "b")]
    try:
        set_user_hwids(first["id"], 2)
        set_user_hwids(second["id"], 0)

        response = client.get(
            "/api/users",
            headers=auth_headers(_login(actor["username"], actor["password"])),
            params={"search": tag, "sort": "-hwid_count,-username"},
        )

        assert response.status_code == status.HTTP_200_OK, response.text
        listed = [(user["username"], user["hwid_count"]) for user in response.json()["users"]]
        if sees_counts:
            assert listed == [(first["username"], 2), (second["username"], 0)]
        else:
            assert listed == [(second["username"], None), (first["username"], None)]
    finally:
        for user in (first, second):
            set_user_hwids(user["id"], 0)
            client.delete(f"/api/user/by-id/{user['id']}", headers=auth_headers(access_token))
        delete_admin(access_token, actor["username"])
        client.delete(f"/api/admin-role/{role['id']}", headers=auth_headers(access_token))


@pytest.mark.parametrize(
    "hwids_read", [pytest.param(True, id="hwids-read-true"), pytest.param(SCOPE_OWN, id="hwids-read-own")]
)
def test_an_own_scope_admin_never_sees_or_sorts_by_other_admins_device_counts(access_token, hwids_read):
    role = _role(access_token, {"users": {"read": SCOPE_OWN}, "hwids": {"read": hwids_read}})
    actor = create_admin(access_token, role_id=role["id"])
    tag = unique_name("hwid_own")
    mine = create_user(access_token, username=f"{tag}_mine")
    others = [create_user(access_token, username=f"{tag}_other_{index}") for index in range(3)]
    try:
        _hand_user_to(mine["id"], actor["id"])
        set_user_hwids(mine["id"], 1)
        for devices, user in enumerate(others, start=2):
            set_user_hwids(user["id"], devices)

        token = _login(actor["username"], actor["password"])
        for sort in ("hwid_count", "-hwid_count"):
            response = client.get(
                "/api/users",
                headers=auth_headers(token),
                params={"search": tag, "sort": sort, "offset": 0, "limit": 10},
            )
            assert response.status_code == status.HTTP_200_OK, response.text
            body = response.json()
            assert body["total"] == 1
            assert [(user["username"], user["hwid_count"]) for user in body["users"]] == [(mine["username"], 1)]
    finally:
        for user in (mine, *others):
            set_user_hwids(user["id"], 0)
            client.delete(f"/api/user/by-id/{user['id']}", headers=auth_headers(access_token))
        delete_admin(access_token, actor["username"])
        client.delete(f"/api/admin-role/{role['id']}", headers=auth_headers(access_token))


Call = tuple[str, str, dict]


def _hwids_read_change(access_token: str, role_id: int, granted: bool) -> Call:
    permissions = {"users": {"read": SCOPE_ALL}}
    if granted:
        permissions["hwids"] = {"read": True}
    return (
        "PUT",
        f"/api/admin-role/{role_id}",
        {"headers": auth_headers(access_token), "json": {"permissions": permissions}},
    )


def _users_list(token: str, tag: str) -> Call:
    return (
        "GET",
        "/api/users",
        {"headers": auth_headers(token), "params": {"search": tag, "sort": "-hwid_count,-username"}},
    )


def _send(call: Call):
    method, url, kwargs = call
    return client.request(method, url, **kwargs)


def _listed(response) -> list[tuple[str, int | None]]:
    assert response.status_code == status.HTTP_200_OK, response.text
    return [(user["username"], user["hwid_count"]) for user in response.json()["users"]]


@pytest.mark.parametrize("granted_after", [pytest.param(False, id="revoke"), pytest.param(True, id="grant")])
def test_lists_racing_a_hwids_read_change_never_leak_or_mix_device_counts(access_token, granted_after):
    reader_role = _role(access_token, {"users": {"read": SCOPE_ALL}, "hwids": {"read": True}})
    plain_role = _role(access_token, {"users": {"read": SCOPE_ALL}})
    reader = create_admin(access_token, role_id=reader_role["id"])
    plain = create_admin(access_token, role_id=plain_role["id"])
    tag = unique_name("hwid_race")
    users = [create_user(access_token, username=f"{tag}_{name}") for name in ("a", "b", "c")]
    try:
        for devices, user in zip((3, 1, 0), users):
            set_user_hwids(user["id"], devices)
        counted = [(user["username"], devices) for user, devices in zip(users, (3, 1, 0))]
        hidden = [(user["username"], None) for user in reversed(users)]
        reader_token = _login(reader["username"], reader["password"])
        plain_token = _login(plain["username"], plain["password"])
        assert _send(_hwids_read_change(access_token, reader_role["id"], not granted_after)).status_code == 200

        for _ in range(3):
            calls = [_users_list(token, tag) for _ in range(4) for token in (reader_token, plain_token)]
            calls.insert(3, _hwids_read_change(access_token, reader_role["id"], granted_after))
            responses = send_concurrently(calls)

            role_change = responses.pop(3)
            assert role_change.status_code == status.HTTP_200_OK, role_change.text
            for index, response in enumerate(responses):
                assert _listed(response) in ([hidden] if index % 2 else [counted, hidden])

            settled = [_listed(_send(_users_list(token, tag))) for token in (reader_token, plain_token)]
            assert settled == [counted if granted_after else hidden, hidden]
            assert _send(_hwids_read_change(access_token, reader_role["id"], not granted_after)).status_code == 200
    finally:
        for user in users:
            set_user_hwids(user["id"], 0)
            client.delete(f"/api/user/by-id/{user['id']}", headers=auth_headers(access_token))
        delete_admin(access_token, reader["username"])
        delete_admin(access_token, plain["username"])
        for role in (reader_role, plain_role):
            client.delete(f"/api/admin-role/{role['id']}", headers=auth_headers(access_token))
