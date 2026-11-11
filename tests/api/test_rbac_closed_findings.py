import asyncio

import pytest
from fastapi import status
from sqlalchemy import select

from app.db.models import User
from app.models.admin import AdminDetails, AdminRoleData
from app.telegram.handlers.admin import bulk_actions
from app.telegram.utils.filters import IsScopeAll
from tests.api import TestSession, client
from tests.api.helpers import auth_headers, create_admin, create_user, delete_admin, delete_user
from tests.api.scoped_admin import ScopedAdmin

OWN = {"scope": 1}
OWN_SCOPED_USERS = {"users": {"create": True, "read": OWN, "update": OWN, "delete": OWN}}


async def _row(user_id: int) -> tuple:
    async with TestSession() as db:
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
        return user.expire, user.data_limit, user.admin_id


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        pytest.param("POST", "/api/users/bulk/expire", {"amount": 86400}, id="bulk-expire"),
        pytest.param("POST", "/api/users/bulk/data_limit", {"amount": 1024**3}, id="bulk-data-limit"),
        pytest.param("POST", "/api/users/bulk/proxy_settings", {"method": "aes-128-gcm"}, id="bulk-proxy"),
        pytest.param("DELETE", "/api/users/expired", None, id="delete-expired"),
    ],
)
def test_an_admin_scoped_to_its_own_users_cannot_run_panel_wide_bulk_operations(access_token, method, path, body):
    scoped = ScopedAdmin(access_token, OWN_SCOPED_USERS)
    stranger = create_user(access_token, payload={"expire": "2020-01-01T00:00:00Z"})
    try:
        before = asyncio.run(_row(stranger["id"]))
        response = client.request(method, path, headers=auth_headers(scoped.token), json=body)
        assert response.status_code == status.HTTP_403_FORBIDDEN, response.text
        assert asyncio.run(_row(stranger["id"])) == before
    finally:
        delete_user(access_token, stranger["username"])
        scoped.close()


TELEGRAM_PANEL_WIDE_HANDLERS = {
    "bulk_actions": ("users", "update"),
    "delete_expired": ("users", "delete"),
    "delete_expired_done": ("users", "delete"),
    "modify_expiry": ("users", "update"),
    "modify_expiry_done": ("users", "update"),
    "modify_data_limit": ("users", "update"),
    "modify_data_limit_done": ("users", "update"),
}


def test_every_panel_wide_telegram_bulk_handler_requires_scope_all():
    gates = {
        handler.callback.__name__: [
            (gate.callback.resource, gate.callback.action)
            for gate in handler.filters or []
            if isinstance(gate.callback, IsScopeAll)
        ]
        for handler in bulk_actions.router.callback_query.handlers
    }
    for name, required in TELEGRAM_PANEL_WIDE_HANDLERS.items():
        assert gates[name] == [required], name


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["update", "delete"])
async def test_the_telegram_bulk_menu_refuses_an_admin_scoped_to_its_own_users(action):
    admin = AdminDetails(
        id=7,
        username="reseller",
        role=AdminRoleData(id=3, name="reseller", is_owner=False, permissions=OWN_SCOPED_USERS),
    )
    assert await IsScopeAll("users", action)(None, admin=admin) is False


def test_the_seeded_operator_role_cannot_move_users_between_admins(access_token):
    operator = create_admin(access_token, role_id=3)
    target = create_admin(access_token, role_id=3)
    token = client.post(
        "/api/admin/token",
        data={"username": operator["username"], "password": operator["password"], "grant_type": "password"},
    ).json()["access_token"]
    own = create_user(token)
    try:
        response = client.put(
            f"/api/user/by-id/{own['id']}/set_owner",
            params={"admin_username": target["username"]},
            headers=auth_headers(token),
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN, response.text
        assert asyncio.run(_row(own["id"]))[2] == operator["id"]
    finally:
        delete_user(access_token, own["username"])
        delete_admin(access_token, operator["username"])
        delete_admin(access_token, target["username"])


def test_a_set_owner_grant_scoped_to_own_users_moves_only_the_admins_own_users(access_token):
    mover = ScopedAdmin(
        access_token,
        {"users": {"create": True, "read": OWN, "set_owner": OWN}, "admins": {"read": True, "update": True}},
    )
    target = create_admin(access_token, role_id=3)
    own = create_user(mover.token)
    stranger = create_user(access_token)
    try:
        moved = client.put(
            f"/api/user/by-id/{own['id']}/set_owner",
            params={"admin_username": target["username"]},
            headers=auth_headers(mover.token),
        )
        assert moved.status_code == status.HTTP_200_OK, moved.text
        assert asyncio.run(_row(own["id"]))[2] == target["id"]
        taken = client.put(
            f"/api/user/by-id/{stranger['id']}/set_owner",
            params={"admin_username": target["username"]},
            headers=auth_headers(mover.token),
        )
        assert taken.status_code == status.HTTP_404_NOT_FOUND, taken.text
        assert asyncio.run(_row(stranger["id"]))[2] != target["id"]
    finally:
        delete_user(access_token, own["username"])
        delete_user(access_token, stranger["username"])
        delete_admin(access_token, target["username"])
        mover.close()
