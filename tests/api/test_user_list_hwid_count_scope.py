import asyncio

import pytest
from fastapi import status
from sqlalchemy import update

from app.db.models import User
from tests.api import TestSession, client
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
        json={"name": unique_name("hwid_count_role"), "permissions": permissions, "limits": {}, "features": {}, "access": {}},
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


def test_an_own_scope_admin_never_sees_or_sorts_by_other_admins_device_counts(access_token):
    role = _role(access_token, {"users": {"read": SCOPE_OWN}, "hwids": {"read": True}})
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
