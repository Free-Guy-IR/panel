import asyncio

from fastapi import status

from app.db.models import ConnectionRestriction, UserConnectionLimit, UserConnectionState
from tests.api import TestSession, client
from tests.api.helpers import (
    auth_headers,
    create_admin,
    create_user,
    delete_admin,
    delete_user,
    unique_name,
)


def _login(username: str, password: str) -> str:
    response = client.post(
        "/api/admin/token",
        data={"username": username, "password": password, "grant_type": "password"},
    )
    assert response.status_code == status.HTTP_200_OK
    return response.json()["access_token"]


def _create_scoped_role(access_token: str) -> dict:
    response = client.post(
        "/api/admin-role",
        headers=auth_headers(access_token),
        json={
            "name": unique_name("conn_limit_own"),
            "permissions": {
                "users": {
                    "create": True,
                    "read": {"scope": 1},
                    "read_simple": {"scope": 1},
                    "update": {"scope": 1},
                    "delete": {"scope": 1},
                },
                "settings": {"read": True, "update": True},
            },
            "limits": {},
            "features": {},
            "access": {},
        },
    )
    assert response.status_code == status.HTTP_201_CREATED
    return response.json()


def _seed_connection_rows(own_user_id: int, other_user_id: int) -> None:
    async def _seed():
        async with TestSession() as session:
            session.add(
                UserConnectionState(
                    user_id=own_user_id,
                    devices=4,
                    verdict="over_limit",
                    reasons=[],
                    details={"real_groups": ["203.0.113.10"]},
                )
            )
            session.add(
                UserConnectionState(
                    user_id=other_user_id,
                    devices=9,
                    verdict="over_limit",
                    reasons=[],
                    details={"real_groups": ["198.51.100.20"]},
                )
            )
            session.add(UserConnectionLimit(user_id=own_user_id, ip_limit=2, exempt=False, note="own"))
            session.add(UserConnectionLimit(user_id=other_user_id, ip_limit=8, exempt=True, note="other"))
            session.add(ConnectionRestriction(user_id=own_user_id, ip_count=4, ip_limit=2, active=True))
            session.add(ConnectionRestriction(user_id=other_user_id, ip_count=9, ip_limit=3, active=True))
            await session.commit()

    asyncio.run(_seed())


def test_connection_limit_endpoints_hide_another_admins_users(access_token):
    role = _create_scoped_role(access_token)
    own_admin = create_admin(access_token, role_id=role["id"])
    other_admin = create_admin(access_token, role_id=role["id"])
    own_user = None
    other_user = None

    try:
        own_token = _login(own_admin["username"], own_admin["password"])
        other_token = _login(other_admin["username"], other_admin["password"])
        own_user = create_user(own_token)
        other_user = create_user(other_token)
        _seed_connection_rows(own_user["id"], other_user["id"])

        own_headers = auth_headers(own_token)

        states = client.get("/api/connection-limit/states", headers=own_headers)
        assert states.status_code == status.HTTP_200_OK
        state_ids = {row["user_id"] for row in states.json()["states"]}
        assert own_user["id"] in state_ids
        assert other_user["id"] not in state_ids

        by_user = client.get(
            "/api/connection-limit/states/by-user",
            headers=own_headers,
            params={"user_ids": [own_user["id"], other_user["id"]]},
        )
        assert by_user.status_code == status.HTTP_200_OK
        by_user_ids = {row["user_id"] for row in by_user.json()["states"]}
        assert by_user_ids == {own_user["id"]}

        overrides = client.get("/api/connection-limit/overrides", headers=own_headers)
        assert overrides.status_code == status.HTTP_200_OK
        override_ids = {row["user_id"] for row in overrides.json()["overrides"]}
        assert own_user["id"] in override_ids
        assert other_user["id"] not in override_ids

        forbidden_override = client.put(
            f"/api/connection-limit/overrides/{other_user['id']}",
            headers=own_headers,
            json={"ip_limit": 1, "exempt": False},
        )
        assert forbidden_override.status_code == status.HTTP_404_NOT_FOUND

        allowed_override = client.put(
            f"/api/connection-limit/overrides/{own_user['id']}",
            headers=own_headers,
            json={"ip_limit": 3, "exempt": False, "note": "mine"},
        )
        assert allowed_override.status_code == status.HTTP_200_OK
        assert allowed_override.json()["ip_limit"] == 3

        forbidden_clear = client.delete(
            f"/api/connection-limit/overrides/{other_user['id']}",
            headers=own_headers,
        )
        assert forbidden_clear.status_code == status.HTTP_404_NOT_FOUND

        forbidden_addresses = client.get(
            f"/api/connection-limit/addresses/{other_user['id']}",
            headers=own_headers,
        )
        assert forbidden_addresses.status_code == status.HTTP_404_NOT_FOUND

        own_addresses = client.get(
            f"/api/connection-limit/addresses/{own_user['id']}",
            headers=own_headers,
        )
        assert own_addresses.status_code == status.HTTP_200_OK
        assert any(item["address"] == "203.0.113.10" for item in own_addresses.json()["addresses"])

        violations = client.get("/api/connection-limit/violations", headers=own_headers)
        assert violations.status_code == status.HTTP_200_OK
        violation_ids = {row["user_id"] for row in violations.json()["violations"]}
        assert own_user["id"] in violation_ids
        assert other_user["id"] not in violation_ids
    finally:
        if own_user is not None:
            delete_user(access_token, own_user["username"])
        if other_user is not None:
            delete_user(access_token, other_user["username"])
        delete_admin(access_token, own_admin["username"])
        delete_admin(access_token, other_admin["username"])
        client.delete(f"/api/admin-role/{role['id']}", headers=auth_headers(access_token))
