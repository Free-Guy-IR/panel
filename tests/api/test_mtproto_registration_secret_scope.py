import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from app.db.models import User
from tests.api import TestSession, app, client
from tests.api.helpers import auth_headers, create_user, delete_user, unique_name

OTHER_SECRET = "ab" * 16


def _role(access_token: str) -> dict:
    response = client.post(
        "/api/admin-role",
        headers=auth_headers(access_token),
        json={
            "name": unique_name("cores_reader"),
            "permissions": {
                "cores": {"read": True, "read_simple": True},
                "users": {"create": True, "read": {"scope": 1}, "read_simple": {"scope": 1}},
            },
            "limits": {},
            "features": {"can_use_reset_strategy": True, "can_use_next_plan": True},
            "access": {"require_template": False, "allowed_template_ids": None, "allowed_group_ids": None},
            "hwid": {"mode": "use_global", "enabled": True, "forced": False},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.asyncio
async def test_scoped_admin_cannot_read_another_admins_user_secret(access_token):
    core = client.post(
        "/api/core",
        headers=auth_headers(access_token),
        json={
            "name": unique_name("mt"),
            "config": {"instances": [{"tag": unique_name("plain"), "port": 8449, "mode": "plain"}]},
            "type": "mtproto",
            "exclude_inbound_tags": [],
            "fallbacks_inbound_tags": [],
        },
    )
    assert core.status_code == 201, core.text
    core = core.json()
    tag = core["config"]["instances"][0]["tag"]
    role = _role(access_token)
    username = unique_name("scoped")
    password = "Scoped#12abCD"
    created = client.post(
        "/api/admin",
        headers=auth_headers(access_token),
        json={"username": username, "password": password, "role_id": role["id"]},
    )
    assert created.status_code == 201, created.text
    victim = create_user(access_token, payload={"username": unique_name("victim")})
    async with TestSession() as db:
        await db.execute(
            update(User).where(User.id == victim["id"]).values(proxy_settings={"mtproto": {"secret": OTHER_SECRET}})
        )
        await db.commit()
    try:
        token = client.post(
            "/api/admin/token", data={"username": username, "password": password, "grant_type": "password"}
        ).json()["access_token"]
        response = client.get(f"/api/core/{core['id']}/mtproto/{tag}/registration-secret", headers=auth_headers(token))
        assert response.status_code != 200 or OTHER_SECRET not in response.json()["secret"], response.json()
    finally:
        delete_user(access_token, victim["username"])
        client.delete(f"/api/admin/{username}", headers=auth_headers(access_token))
        client.delete(f"/api/admin-role/{role['id']}", headers=auth_headers(access_token))
        client.delete(f"/api/core/{core['id']}", headers=auth_headers(access_token))


OWN_SECRET = "cd" * 16


def _mtproto_core(access_token: str, instance: dict) -> dict:
    response = client.post(
        "/api/core",
        headers=auth_headers(access_token),
        json={
            "name": unique_name("mt"),
            "config": {"instances": [instance]},
            "type": "mtproto",
            "exclude_inbound_tags": [],
            "fallbacks_inbound_tags": [],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.asyncio
async def test_scoped_admin_gets_a_working_secret_of_its_own_user(access_token):
    tag = unique_name("plain")
    core = _mtproto_core(access_token, {"tag": tag, "port": 8450, "mode": "plain"})
    role = _role(access_token)
    username = unique_name("scoped")
    password = "Scoped#12abCD"
    created = client.post(
        "/api/admin",
        headers=auth_headers(access_token),
        json={"username": username, "password": password, "role_id": role["id"]},
    )
    assert created.status_code == 201, created.text
    token = client.post(
        "/api/admin/token", data={"username": username, "password": password, "grant_type": "password"}
    ).json()["access_token"]
    other = create_user(access_token, payload={"username": unique_name("other")})
    own = create_user(token, payload={"username": unique_name("own")})
    async with TestSession() as db:
        await db.execute(
            update(User).where(User.id == other["id"]).values(proxy_settings={"mtproto": {"secret": OTHER_SECRET}})
        )
        await db.execute(
            update(User).where(User.id == own["id"]).values(proxy_settings={"mtproto": {"secret": OWN_SECRET}})
        )
        await db.commit()
    try:
        response = client.get(f"/api/core/{core['id']}/mtproto/{tag}/registration-secret", headers=auth_headers(token))
        assert response.status_code == 200, response.text
        assert response.json()["secret"] == "dd" + OWN_SECRET
    finally:
        delete_user(access_token, own["username"])
        delete_user(access_token, other["username"])
        client.delete(f"/api/admin/{username}", headers=auth_headers(access_token))
        client.delete(f"/api/admin-role/{role['id']}", headers=auth_headers(access_token))
        client.delete(f"/api/core/{core['id']}", headers=auth_headers(access_token))


@pytest.mark.asyncio
async def test_a_non_ascii_fake_tls_domain_is_a_client_error(access_token):
    tag = unique_name("ft")
    core = _mtproto_core(access_token, {"tag": tag, "port": 8451, "fake_tls_domain": "bücher.example"})
    holder = create_user(access_token, payload={"username": unique_name("holder")})
    async with TestSession() as db:
        await db.execute(
            update(User).where(User.id == holder["id"]).values(proxy_settings={"mtproto": {"secret": OWN_SECRET}})
        )
        await db.commit()
    try:
        probe = TestClient(app, raise_server_exceptions=False)
        response = probe.get(
            f"/api/core/{core['id']}/mtproto/{tag}/registration-secret", headers=auth_headers(access_token)
        )
        assert response.status_code == 409, response.text
        assert "ASCII" in response.json()["detail"]
    finally:
        delete_user(access_token, holder["username"])
        client.delete(f"/api/core/{core['id']}", headers=auth_headers(access_token))
