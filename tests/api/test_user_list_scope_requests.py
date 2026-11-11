import pytest
from fastapi import status

from tests.api import client
from tests.api.helpers import auth_headers, create_admin, create_user, delete_admin, unique_name

SCOPE_OWN = {"scope": 1}
SCOPE_ALL = {"scope": 2}


def _login(username: str, password: str) -> str:
    response = client.post(
        "/api/admin/token",
        data={"username": username, "password": password, "grant_type": "password"},
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    return response.json()["access_token"]


def _create_role(access_token: str, users_permissions: dict) -> dict:
    response = client.post(
        "/api/admin-role",
        headers=auth_headers(access_token),
        json={
            "name": unique_name("role_list_scope"),
            "permissions": {"users": users_permissions},
            "limits": {},
            "features": {},
            "access": {},
        },
    )
    assert response.status_code == status.HTTP_201_CREATED, response.text
    return response.json()


def _listed_usernames(path: str, token: str, search: str) -> set[str]:
    response = client.get(path, headers=auth_headers(token), params={"search": search})
    assert response.status_code == status.HTTP_200_OK, response.text
    return {user["username"] for user in response.json()["users"]}


@pytest.mark.parametrize(
    "read_scope, read_simple_scope",
    [(SCOPE_OWN, SCOPE_ALL), (SCOPE_ALL, SCOPE_OWN)],
    ids=["read-own_read-simple-all", "read-all_read-simple-own"],
)
def test_each_user_list_filters_by_its_own_scope(access_token, read_scope, read_simple_scope):
    role = _create_role(access_token, {"create": True, "read": read_scope, "read_simple": read_simple_scope})
    actor = create_admin(access_token, role_id=role["id"])
    other = create_admin(access_token, role_id=role["id"])
    marker = unique_name("sl")
    own_name, other_name = f"{marker}_own", f"{marker}_other"
    created = []
    try:
        actor_token = _login(actor["username"], actor["password"])
        created.append(create_user(actor_token, username=own_name))
        created.append(create_user(_login(other["username"], other["password"]), username=other_name))

        listed = _listed_usernames("/api/users", actor_token, marker)
        simple = _listed_usernames("/api/users/simple", actor_token, marker)

        assert listed == ({own_name, other_name} if read_scope is SCOPE_ALL else {own_name})
        assert simple == ({own_name, other_name} if read_simple_scope is SCOPE_ALL else {own_name})
    finally:
        for user in created:
            client.delete(f"/api/user/by-id/{user['id']}", headers=auth_headers(access_token))
        delete_admin(access_token, actor["username"])
        delete_admin(access_token, other["username"])
        client.delete(f"/api/admin-role/{role['id']}", headers=auth_headers(access_token))
