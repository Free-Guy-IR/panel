from datetime import UTC, datetime, timedelta

from fastapi import status
from sqlalchemy import delete, select, update

from app.db.models import Admin, NodeUserUsage, User, UserStatus
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


def _role(access_token: str, permissions: dict, *, allowed_group_ids=None, limits=None) -> dict:
    body = {"name": unique_name("adm_scope_role"), "permissions": permissions}
    if allowed_group_ids is not None:
        body["access"] = {
            "require_template": False,
            "allowed_template_ids": None,
            "allowed_group_ids": allowed_group_ids,
        }
    if limits is not None:
        body["limits"] = limits
    response = client.post("/api/admin-role", headers=auth_headers(access_token), json=body)
    assert response.status_code == status.HTTP_201_CREATED, response.text
    return response.json()


class _Scene:
    def __init__(self, access_token: str):
        self.access_token = access_token
        self.core = create_core(access_token)
        self.allowed = create_group(access_token, name=unique_name("adm_allowed"))
        self.forbidden = create_group(access_token, name=unique_name("adm_forbidden"))
        self.seller = create_admin(access_token)
        self.roles: list[dict] = []
        self.admins: list[dict] = [self.seller]
        self.users: list[dict] = []

    def caller(self, permissions: dict, *, restricted: bool = True, limits=None) -> tuple[dict, dict]:
        role = _role(
            self.access_token,
            permissions,
            allowed_group_ids=[self.allowed["id"]] if restricted else None,
            limits=limits,
        )
        self.roles.append(role)
        admin = create_admin(self.access_token, role_id=role["id"])
        self.admins.append(admin)
        return admin, auth_headers(_login(admin["username"], admin["password"]))

    async def seller_user(self, group: dict, **values) -> dict:
        user = create_user(self.access_token, group_ids=[group["id"]], payload={"username": unique_name("adm_u")})
        async with TestSession() as session:
            seller_id = (
                await session.execute(select(Admin.id).where(Admin.username == self.seller["username"]))
            ).scalar_one()
            await session.execute(update(User).where(User.id == user["id"]).values(admin_id=seller_id, **values))
            await session.commit()
        self.users.append(user)
        return user

    async def status_of(self, user: dict):
        async with TestSession() as session:
            return (await session.execute(select(User.status).where(User.id == user["id"]))).scalar_one_or_none()

    async def cleanup(self):
        ids = [user["id"] for user in self.users]
        async with TestSession() as session:
            await session.execute(delete(NodeUserUsage).where(NodeUserUsage.user_id.in_(ids)))
            await session.commit()
        for user in self.users:
            client.delete(f"/api/user/by-id/{user['id']}", headers=auth_headers(self.access_token))
        for admin in self.admins:
            delete_admin(self.access_token, admin["username"])
        for role in self.roles:
            client.delete(f"/api/admin-role/{role['id']}", headers=auth_headers(self.access_token))
        delete_group(self.access_token, self.allowed["id"])
        delete_group(self.access_token, self.forbidden["id"])
        delete_core(self.access_token, self.core["id"])


async def test_a_group_restricted_admin_disables_and_activates_only_users_inside_its_groups(access_token):
    scene = _Scene(access_token)
    try:
        _, headers = scene.caller({"admins": {"read": True, "update": True}, "users": {"read": True}})
        visible = await scene.seller_user(scene.allowed)
        hidden = await scene.seller_user(scene.forbidden)
        seller = scene.seller["username"]

        disabled = client.post(f"/api/admin/{seller}/users/disable", headers=headers)
        assert disabled.status_code == status.HTTP_200_OK, disabled.text
        assert (await scene.status_of(visible), await scene.status_of(hidden)) == (
            UserStatus.disabled,
            UserStatus.active,
        )

        async with TestSession() as session:
            await session.execute(update(User).where(User.id == hidden["id"]).values(status=UserStatus.disabled))
            await session.commit()
        activated = client.post(f"/api/admin/{seller}/users/activate", headers=headers)
        assert activated.status_code == status.HTTP_200_OK, activated.text
        assert (await scene.status_of(visible), await scene.status_of(hidden)) == (
            UserStatus.active,
            UserStatus.disabled,
        )
    finally:
        await scene.cleanup()


async def test_a_group_restricted_admin_deletes_only_users_inside_its_groups(access_token):
    scene = _Scene(access_token)
    try:
        _, headers = scene.caller({"admins": {"read": True, "delete": True}, "users": {"read": True}})
        visible = await scene.seller_user(scene.allowed)
        hidden = await scene.seller_user(scene.forbidden)

        removed = client.delete(f"/api/admin/{scene.seller['username']}/users", headers=headers)
        assert removed.status_code == status.HTTP_200_OK, removed.text
        assert (await scene.status_of(visible), await scene.status_of(hidden)) == (None, UserStatus.active)
    finally:
        await scene.cleanup()


async def test_a_group_restricted_admin_sees_admin_user_counts_and_usage_only_for_its_groups(access_token):
    scene = _Scene(access_token)
    try:
        _, headers = scene.caller({"admins": {"read": True}, "users": {"read": True}})
        visible = await scene.seller_user(scene.allowed)
        hidden = await scene.seller_user(scene.forbidden)
        at = (datetime.now(UTC) - timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)
        async with TestSession() as session:
            session.add_all(
                [
                    NodeUserUsage(created_at=at, user_id=visible["id"], node_id=None, used_traffic=100),
                    NodeUserUsage(created_at=at, user_id=hidden["id"], node_id=None, used_traffic=10_000),
                ]
            )
            await session.commit()
        seller = scene.seller["username"]
        window = {
            "period": "day",
            "start": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
            "end": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        }

        def view(request_headers) -> tuple[int, int]:
            admins = client.get("/api/admins", headers=request_headers, params={"username": seller})
            usage = client.get(f"/api/admin/{seller}/usage", headers=request_headers, params=window)
            assert (admins.status_code, usage.status_code) == (200, 200), (admins.text, usage.text)
            [row] = [row for row in admins.json()["admins"] if row["username"] == seller]
            traffic = sum(stat["total_traffic"] for stats in usage.json()["stats"].values() for stat in stats)
            return row["total_users"], traffic

        assert view(auth_headers(access_token)) == (2, 10_100)
        assert view(headers) == (1, 100)
    finally:
        await scene.cleanup()


async def test_the_max_users_quota_counts_every_owned_user_even_outside_the_allowed_groups(access_token):
    scene = _Scene(access_token)
    try:
        caller, headers = scene.caller(
            {"users": {"create": True, "read": {"scope": 1}}, "groups": {"read": True}}, limits={"max_users": 2}
        )
        visible = await scene.seller_user(scene.allowed)
        hidden = await scene.seller_user(scene.forbidden)
        async with TestSession() as session:
            caller_id = (
                await session.execute(select(Admin.id).where(Admin.username == caller["username"]))
            ).scalar_one()
            await session.execute(
                update(User).where(User.id.in_([visible["id"], hidden["id"]])).values(admin_id=caller_id)
            )
            await session.commit()

        listed = client.get("/api/users", headers=headers).json()["users"]
        created = client.post(
            "/api/user",
            headers=headers,
            json={
                "username": unique_name("adm_third"),
                "proxy_settings": {},
                "data_limit_reset_strategy": "no_reset",
                "status": "active",
                "group_ids": [scene.allowed["id"]],
            },
        )
        assert [user["id"] for user in listed] == [visible["id"]]
        assert created.status_code == status.HTTP_400_BAD_REQUEST, created.text
        assert "User limit reached" in created.json()["detail"]
    finally:
        await scene.cleanup()


async def test_clearing_usage_data_is_owner_only(access_token):
    scene = _Scene(access_token)
    try:
        _, headers = scene.caller({"nodes": {"read": True, "delete": True}}, restricted=False)
        window = {"start": "1990-01-01T00:00:00+00:00", "end": "1990-01-02T00:00:00+00:00"}

        refused = client.delete("/api/nodes/clear_usage_data/node_user_usages", headers=headers, params=window)
        allowed = client.delete(
            "/api/nodes/clear_usage_data/node_user_usages", headers=auth_headers(access_token), params=window
        )

        assert refused.status_code == status.HTTP_403_FORBIDDEN, refused.text
        assert allowed.status_code == status.HTTP_200_OK, allowed.text
    finally:
        await scene.cleanup()
