import pytest
from fastapi import HTTPException, status

from app.db.models import ConnectionRestriction, UserConnectionLimit, UserConnectionState
from app.fork.routers import connection_limit
from app.models.admin import AdminDetails, AdminRoleData
from tests.api import TestSession, client
from tests.api.helpers import (
    auth_headers,
    create_admin,
    create_core,
    create_group,
    create_user,
    delete_admin,
    delete_core,
    delete_group,
    unique_name,
)


def _login(username: str, password: str) -> str:
    response = client.post(
        "/api/admin/token", data={"username": username, "password": password, "grant_type": "password"}
    )
    assert response.status_code == status.HTTP_200_OK
    return response.json()["access_token"]


async def _seed(user_id: int, address: str) -> None:
    async with TestSession() as session:
        session.add(
            UserConnectionState(
                user_id=user_id, devices=5, verdict="over_limit", reasons=[], details={"real_groups": [address]}
            )
        )
        session.add(UserConnectionLimit(user_id=user_id, ip_limit=2, exempt=False, note="seeded"))
        session.add(ConnectionRestriction(user_id=user_id, ip_count=5, ip_limit=2, active=True))
        await session.commit()


async def test_a_group_restricted_admin_sees_and_changes_connection_data_only_for_users_it_can_see(access_token):
    core = create_core(access_token)
    allowed = create_group(access_token, name=unique_name("cl_allowed"))
    forbidden = create_group(access_token, name=unique_name("cl_forbidden"))
    role = client.post(
        "/api/admin-role",
        headers=auth_headers(access_token),
        json={
            "name": unique_name("cl_role"),
            "permissions": {"users": {"read": True, "update": True}, "settings": {"read": True, "update": True}},
            "access": {"require_template": False, "allowed_template_ids": None, "allowed_group_ids": [allowed["id"]]},
        },
    )
    assert role.status_code == status.HTTP_201_CREATED, role.text
    role_data = role.json()
    admin = create_admin(access_token, role_id=role_data["id"])
    headers = auth_headers(_login(admin["username"], admin["password"]))
    visible = create_user(access_token, group_ids=[allowed["id"]], payload={"username": unique_name("cl_visible")})
    hidden = create_user(access_token, group_ids=[forbidden["id"]], payload={"username": unique_name("cl_hidden")})
    await _seed(visible["id"], "203.0.113.10")
    await _seed(hidden["id"], "198.51.100.20")
    try:
        listed = {
            "states": client.get("/api/connection-limit/states", headers=headers).json()["states"],
            "states by user": client.get(
                "/api/connection-limit/states/by-user",
                headers=headers,
                params={"user_ids": [visible["id"], hidden["id"]]},
            ).json()["states"],
            "overrides": client.get("/api/connection-limit/overrides", headers=headers).json()["overrides"],
            "violations": client.get("/api/connection-limit/violations", headers=headers).json()["violations"],
        }
        leaked = {name: sorted(row["user_id"] for row in rows) for name, rows in listed.items()}
        assert leaked == {name: [visible["id"]] for name in listed}, leaked

        reached = {
            "addresses": client.get(f"/api/connection-limit/addresses/{hidden['id']}", headers=headers).status_code,
            "set override": client.put(
                f"/api/connection-limit/overrides/{hidden['id']}", headers=headers, json={"ip_limit": 9}
            ).status_code,
            "clear override": client.delete(
                f"/api/connection-limit/overrides/{hidden['id']}", headers=headers
            ).status_code,
        }
        assert reached == {name: status.HTTP_404_NOT_FOUND for name in reached}, reached

        restricted_admin = AdminDetails(
            id=admin.get("id"),
            username=admin["username"],
            role=AdminRoleData.model_validate(role_data),
        )
        async with TestSession() as db:
            with pytest.raises(HTTPException) as refused:
                await connection_limit.release_user(hidden["id"], forget=False, db=db, admin=restricted_admin)
        assert refused.value.status_code == status.HTTP_404_NOT_FOUND

        assert (
            client.get(f"/api/connection-limit/addresses/{visible['id']}", headers=headers).status_code
            == status.HTTP_200_OK
        )
        assert (
            client.put(
                f"/api/connection-limit/overrides/{visible['id']}", headers=headers, json={"ip_limit": 3}
            ).status_code
            == status.HTTP_200_OK
        )
    finally:
        for user in (visible, hidden):
            client.delete(f"/api/user/by-id/{user['id']}", headers=auth_headers(access_token))
        delete_admin(access_token, admin["username"])
        client.delete(f"/api/admin-role/{role_data['id']}", headers=auth_headers(access_token))
        delete_group(access_token, allowed["id"])
        delete_group(access_token, forbidden["id"])
        delete_core(access_token, core["id"])
