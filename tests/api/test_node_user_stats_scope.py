from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.operation import node as node_operation
from tests.api import client
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
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _role(access_token: str, users_read, allowed_group_ids=None) -> int:
    response = client.post(
        "/api/admin-role",
        headers=auth_headers(access_token),
        json={
            "name": unique_name("stats_role"),
            "permissions": {"users": {"read": users_read}, "nodes": {"stats": True}},
            "access": {
                "require_template": False,
                "allowed_template_ids": None,
                "allowed_group_ids": allowed_group_ids,
            },
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


class _NodeWithOnlineUser:
    async def get_user_online_stats(self, email):
        return SimpleNamespace(value=3)

    async def get_user_online_ip_list(self, email):
        return SimpleNamespace(ips={"203.0.113.9": 1})


def build_world(access_token, monkeypatch):
    node = _NodeWithOnlineUser()
    monkeypatch.setattr(node_operation.node_manager, "get_node", AsyncMock(return_value=node))
    monkeypatch.setattr(node_operation.node_manager, "get_healthy_nodes", AsyncMock(return_value=[(77, node)]))
    core = create_core(access_token)
    allowed = create_group(access_token, name=unique_name("stats_allowed"))
    forbidden = create_group(access_token, name=unique_name("stats_forbidden"))
    own_role = _role(access_token, {"scope": 1})
    group_role = _role(access_token, True, [allowed["id"]])
    own_admin = create_admin(access_token, role_id=own_role)
    group_admin = create_admin(access_token, role_id=group_role)
    visible = create_user(access_token, group_ids=[allowed["id"]], payload={"username": unique_name("stats_vis")})
    hidden = create_user(access_token, group_ids=[forbidden["id"]], payload={"username": unique_name("stats_hid")})
    yield SimpleNamespace(
        own_token=_login(own_admin["username"], own_admin["password"]),
        group_token=_login(group_admin["username"], group_admin["password"]),
        visible=visible,
        hidden=hidden,
    )
    for user in (visible, hidden):
        client.delete(f"/api/user/by-id/{user['id']}", headers=auth_headers(access_token))
    for admin in (own_admin, group_admin):
        delete_admin(access_token, admin["username"])
    for role_id in (own_role, group_role):
        client.delete(f"/api/admin-role/{role_id}", headers=auth_headers(access_token))
    delete_group(access_token, allowed["id"])
    delete_group(access_token, forbidden["id"])
    delete_core(access_token, core["id"])


@pytest.fixture
def world(access_token, monkeypatch):
    yield from build_world(access_token, monkeypatch)


def _online_routes(user_id: int) -> list[str]:
    return [
        f"/api/node/77/online_stats/{user_id}",
        f"/api/node/77/online_stats/{user_id}/ip",
        f"/api/node/online_stats/{user_id}/ip",
    ]


def test_an_own_scope_admin_cannot_read_another_admins_users_online_ips(world, access_token):
    for route in _online_routes(world.hidden["id"]):
        assert client.get(route, headers=auth_headers(access_token)).status_code == 200, route
        response = client.get(route, headers=auth_headers(world.own_token))
        assert response.status_code == 404, (route, response.status_code, response.text)


def test_a_group_restricted_admin_only_reads_online_ips_of_users_it_can_see(world):
    for route in _online_routes(world.visible["id"]):
        response = client.get(route, headers=auth_headers(world.group_token))
        assert response.status_code == 200, (route, response.text)
    for route in _online_routes(world.hidden["id"]):
        response = client.get(route, headers=auth_headers(world.group_token))
        assert response.status_code == 404, (route, response.status_code, response.text)
