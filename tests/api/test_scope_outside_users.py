import pytest
from fastapi import status

from tests.api import client
from tests.api.helpers import auth_headers, create_user, delete_user
from tests.api.scoped_admin import ScopedAdmin

OWN = {"scope": 1}
ALL = {"scope": 2}


def _get(token: str, path: str):
    return client.get(path, headers=auth_headers(token))


@pytest.mark.parametrize(
    ("permissions", "path"),
    [
        pytest.param({"admins": {"read": OWN}}, "/api/admins", id="admins-list"),
        pytest.param({"admins": {"read": OWN}}, "/api/admin/by-id/{admin_id}/usage", id="admins-usage"),
        pytest.param({"nodes": {"read": OWN}}, "/api/nodes", id="nodes-list"),
        pytest.param({"hosts": {"read": OWN}}, "/api/hosts", id="hosts-list"),
        pytest.param({"groups": {"read": OWN}}, "/api/groups", id="groups-list"),
        pytest.param({"cores": {"read": OWN}}, "/api/cores", id="cores-list"),
        pytest.param({"settings": {"read_general": OWN}}, "/api/settings/general", id="settings-general"),
        pytest.param({"system": {"read": OWN}}, "/api/system", id="system"),
    ],
)
def test_scope_own_on_a_resource_without_owners_is_refused(access_token, permissions, path):
    scoped = ScopedAdmin(access_token, permissions)
    try:
        response = _get(scoped.token, path.format(admin_id=scoped.admin["id"]))
        assert response.status_code == status.HTTP_403_FORBIDDEN, response.text
    finally:
        scoped.close()


@pytest.mark.parametrize(
    ("permissions", "path"),
    [
        pytest.param({"admins": {"read": ALL}}, "/api/admins", id="admins-all"),
        pytest.param({"admins": {"read": True}}, "/api/admins", id="admins-true"),
        pytest.param({"nodes": {"read": ALL}}, "/api/nodes", id="nodes-all"),
        pytest.param({"hosts": {"read": True}}, "/api/hosts", id="hosts-true"),
        pytest.param({"api_keys": {"read": OWN}}, "/api/api_keys", id="api-keys-own"),
    ],
)
def test_scopes_that_mean_something_keep_working(access_token, permissions, path):
    scoped = ScopedAdmin(access_token, permissions)
    try:
        response = _get(scoped.token, path)
        assert response.status_code == status.HTTP_200_OK, response.text
    finally:
        scoped.close()


def test_scope_own_on_users_still_lists_only_the_admins_own_users(access_token):
    scoped = ScopedAdmin(access_token, {"users": {"read": OWN, "create": True}})
    stranger = create_user(access_token)
    try:
        response = _get(scoped.token, "/api/users")
        assert response.status_code == status.HTTP_200_OK, response.text
        assert stranger["username"] not in {user["username"] for user in response.json()["users"]}
    finally:
        delete_user(access_token, stranger["username"])
        scoped.close()


def test_scope_own_on_hwids_still_reaches_only_the_admins_own_users(access_token):
    scoped = ScopedAdmin(access_token, {"users": {"read": OWN, "create": True}, "hwids": {"read": OWN}})
    own = create_user(scoped.token)
    stranger = create_user(access_token)
    try:
        mine = _get(scoped.token, f"/api/user/{own['id']}/hwids")
        assert mine.status_code == status.HTTP_200_OK, mine.text
        theirs = _get(scoped.token, f"/api/user/{stranger['id']}/hwids")
        assert theirs.status_code == status.HTTP_404_NOT_FOUND, theirs.text
    finally:
        delete_user(access_token, own["username"])
        delete_user(access_token, stranger["username"])
        scoped.close()
