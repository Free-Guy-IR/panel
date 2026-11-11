import pytest
from fastapi import status
from sqlalchemy import delete

from app.db.models import ProxyHost
from app.operation import host as host_operation
from tests.api import TestSession, client
from tests.api.concurrent_requests import send_concurrently
from tests.api.helpers import auth_headers, create_core, delete_core, get_inbounds, unique_name
from tests.api.scoped_admin import ScopedAdmin

EDITOR = {"hosts": {"read": True, "update": True}}
DELETER = {"hosts": {"read": True, "update": True, "delete": True}}
DELETE_ONLY = {"hosts": {"read": True, "delete": True}}
CREATOR = {"hosts": {"read": True, "create": True, "update": True}}
CREATION_REFUSED = "Permission denied: hosts.create"


def _host_payload(inbound: str) -> dict:
    return {
        "remark": unique_name("delete_perm_host"),
        "address": ["127.0.0.1"],
        "port": 443,
        "sni": ["delete-perm.example.com"],
        "inbound_tag": inbound,
        "priority": 1,
    }


def _host_ids(access_token: str) -> set[int]:
    response = client.get("/api/hosts", headers=auth_headers(access_token))
    assert response.status_code == status.HTTP_200_OK, response.text
    return {item["id"] for item in response.json()}


def _vanished(host_id: int) -> str:
    return f"Host {host_id} was deleted while it was being edited"


def _exists(access_token: str, host_id: int) -> bool:
    return client.get(f"/api/host/{host_id}", headers=auth_headers(access_token)).status_code == status.HTTP_200_OK


@pytest.fixture
def host(access_token):
    core = create_core(access_token, name=unique_name("delete_perm_core"))
    inbound = get_inbounds(access_token)[0]
    response = client.post("/api/host", headers=auth_headers(access_token), json=_host_payload(inbound))
    assert response.status_code == status.HTTP_201_CREATED, response.text
    created = response.json()
    yield created
    client.delete(f"/api/host/{created['id']}", headers=auth_headers(access_token))
    delete_core(access_token, core["id"])


def _delete(token: str, host_id: int, how: str):
    if how == "single":
        return client.delete(f"/api/host/{host_id}", headers=auth_headers(token))
    return client.post("/api/hosts/bulk/delete", headers=auth_headers(token), json={"ids": [host_id]})


@pytest.mark.parametrize("how", ["single", "bulk"])
def test_an_admin_who_can_edit_hosts_cannot_delete_them_without_the_delete_permission(access_token, host, how):
    editor = ScopedAdmin(access_token, EDITOR)
    try:
        response = _delete(editor.token, host["id"], how)
        assert response.status_code == status.HTTP_403_FORBIDDEN, response.text
        assert _exists(access_token, host["id"])
        renamed = {**host, "remark": unique_name("renamed_host")}
        edited = client.put(f"/api/host/{host['id']}", headers=auth_headers(editor.token), json=renamed)
        assert edited.status_code == status.HTTP_200_OK, edited.text
    finally:
        editor.close()


@pytest.mark.parametrize("permissions", [DELETER, DELETE_ONLY], ids=["delete-and-update", "delete-only"])
@pytest.mark.parametrize("how", ["single", "bulk"])
def test_an_admin_with_the_delete_permission_deletes_hosts(access_token, host, how, permissions):
    deleter = ScopedAdmin(access_token, permissions)
    try:
        response = _delete(deleter.token, host["id"], how)
        expected = status.HTTP_204_NO_CONTENT if how == "single" else status.HTTP_200_OK
        assert response.status_code == expected, response.text
        assert not _exists(access_token, host["id"])
    finally:
        deleter.close()


def test_an_admin_who_can_edit_hosts_cannot_create_one_through_the_bulk_edit(access_token, host):
    editor = ScopedAdmin(access_token, EDITOR)
    try:
        before = _host_ids(access_token)
        response = client.put(
            "/api/hosts", headers=auth_headers(editor.token), json=[_host_payload(host["inbound_tag"])]
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN, response.text
        assert response.json()["detail"] == CREATION_REFUSED
        assert _host_ids(access_token) == before
    finally:
        editor.close()


def test_an_admin_who_can_edit_hosts_cannot_create_one_by_naming_an_id_that_does_not_exist(access_token, host):
    editor = ScopedAdmin(access_token, EDITOR)
    try:
        before = _host_ids(access_token)
        missing_id = max(before) + 1000
        response = client.put(
            "/api/hosts",
            headers=auth_headers(editor.token),
            json=[{**_host_payload(host["inbound_tag"]), "id": missing_id}],
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN, response.text
        assert response.json()["detail"] == CREATION_REFUSED
        assert _host_ids(access_token) == before
    finally:
        editor.close()


def test_an_admin_who_can_edit_hosts_still_reorders_existing_ones_through_the_bulk_edit(access_token, host):
    editor = ScopedAdmin(access_token, EDITOR)
    try:
        response = client.put(
            "/api/hosts", headers=auth_headers(editor.token), json=[{**host, "priority": host["priority"] + 1}]
        )
        assert response.status_code == status.HTTP_200_OK, response.text
    finally:
        editor.close()


def test_an_admin_who_can_create_hosts_still_creates_one_through_the_bulk_edit(access_token, host):
    creator = ScopedAdmin(access_token, CREATOR)
    created_ids: set[int] = set()
    try:
        before = _host_ids(access_token)
        response = client.put(
            "/api/hosts", headers=auth_headers(creator.token), json=[_host_payload(host["inbound_tag"])]
        )
        assert response.status_code == status.HTTP_200_OK, response.text
        created_ids = _host_ids(access_token) - before
        assert len(created_ids) == 1
    finally:
        for host_id in created_ids:
            client.delete(f"/api/host/{host_id}", headers=auth_headers(access_token))
        creator.close()


def test_a_host_deleted_after_the_bulk_edit_read_it_is_never_recreated_and_answers_409(access_token, host, monkeypatch):
    editor = ScopedAdmin(access_token, EDITOR)
    real_write = host_operation.crud_modify_hosts

    async def another_admin_deletes_it_first(db, modified_hosts, db_hosts):
        async with TestSession() as other:
            await other.execute(delete(ProxyHost).where(ProxyHost.id == host["id"]))
            await other.commit()
        return await real_write(db, modified_hosts, db_hosts)

    monkeypatch.setattr(host_operation, "crud_modify_hosts", another_admin_deletes_it_first)
    try:
        before = _host_ids(access_token)
        response = client.put(
            "/api/hosts", headers=auth_headers(editor.token), json=[{**host, "priority": host["priority"] + 1}]
        )
        assert response.status_code == status.HTTP_409_CONFLICT, response.text
        assert response.json()["detail"] == _vanished(host["id"])
        assert _host_ids(access_token) == before - {host["id"]}
        remarks = {item["remark"] for item in client.get("/api/hosts", headers=auth_headers(access_token)).json()}
        assert host["remark"] not in remarks
    finally:
        editor.close()


def test_a_bulk_edit_racing_a_delete_never_leaves_a_recreated_host_or_a_server_error(access_token, host):
    editor = ScopedAdmin(access_token, EDITOR)
    try:
        before = _host_ids(access_token)
        edit = ("PUT", "/api/hosts", {"headers": auth_headers(editor.token), "json": [{**host, "priority": 9}]})
        remove = ("DELETE", f"/api/host/{host['id']}", {"headers": auth_headers(access_token)})
        edit_response, remove_response = send_concurrently([edit, remove])

        assert remove_response.status_code == status.HTTP_204_NO_CONTENT, remove_response.text
        assert edit_response.status_code in (
            status.HTTP_200_OK,
            status.HTTP_403_FORBIDDEN,
            status.HTTP_409_CONFLICT,
        ), edit_response.text
        if edit_response.status_code == status.HTTP_403_FORBIDDEN:
            assert edit_response.json()["detail"] == CREATION_REFUSED
        if edit_response.status_code == status.HTTP_409_CONFLICT:
            assert edit_response.json()["detail"] == _vanished(host["id"])
        assert _host_ids(access_token) == before - {host["id"]}
    finally:
        editor.close()


def test_the_seeded_roles_keep_deleting_hosts(access_token, host):
    response = client.get("/api/admin-roles", headers=auth_headers(access_token))
    assert response.status_code == status.HTTP_200_OK, response.text
    roles = {role["name"]: role for role in response.json()["roles"]}
    for name in ("owner", "administrator"):
        assert roles[name]["permissions"]["hosts"]["delete"] is True, roles[name]["permissions"]["hosts"]
