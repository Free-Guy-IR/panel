from sqlalchemy import delete, select, update

from app.db.models import Admin, User
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

GB = 1024**3


def _login(username: str, password: str) -> str:
    response = client.post(
        "/api/admin/token", data={"username": username, "password": password, "grant_type": "password"}
    )
    assert response.status_code == 200
    return response.json()["access_token"]


def _role(access_token: str, *, permissions: dict, limits: dict | None = None, access: dict | None = None) -> dict:
    body = {"name": unique_name("scope_role"), "permissions": permissions}
    if limits is not None:
        body["limits"] = limits
    if access is not None:
        body["access"] = access
    response = client.post("/api/admin-role", headers=auth_headers(access_token), json=body)
    assert response.status_code == 201, response.text
    return response.json()


def _drop_role(access_token: str, role_id: int) -> None:
    client.delete(f"/api/admin-role/{role_id}", headers=auth_headers(access_token))


def test_a_group_restricted_admin_cannot_reach_a_user_hidden_from_its_listing(access_token):
    core = create_core(access_token)
    allowed = create_group(access_token, name=unique_name("scope_allowed"))
    forbidden = create_group(access_token, name=unique_name("scope_forbidden"))
    role = _role(
        access_token,
        permissions={
            "users": {"create": True, "read": True, "update": True, "delete": True, "revoke_sub": True},
            "groups": {"read": True, "read_simple": True},
        },
        access={"require_template": False, "allowed_template_ids": None, "allowed_group_ids": [allowed["id"]]},
    )
    admin = create_admin(access_token, role_id=role["id"])
    token = _login(admin["username"], admin["password"])
    hidden = create_user(access_token, group_ids=[forbidden["id"]], payload={"username": unique_name("scope_hidden")})
    try:
        listed = {u["username"] for u in client.get("/api/users", headers=auth_headers(token)).json()["users"]}
        assert hidden["username"] not in listed
        reached = {
            "GET by-id": client.get(f"/api/user/by-id/{hidden['id']}", headers=auth_headers(token)).status_code,
            "GET by-name": client.get(f"/api/user/{hidden['username']}", headers=auth_headers(token)).status_code,
            "PUT note": client.put(
                f"/api/user/by-id/{hidden['id']}", headers=auth_headers(token), json={"note": "reached"}
            ).status_code,
            "revoke_sub": client.post(
                f"/api/user/by-id/{hidden['id']}/revoke_sub", headers=auth_headers(token)
            ).status_code,
            "bulk disable": client.post(
                "/api/users/bulk/disable", headers=auth_headers(token), json={"ids": [hidden["id"]]}
            ).status_code,
        }
        succeeded = {name: code for name, code in reached.items() if code < 300}
        assert not succeeded, f"hidden from the listing, yet reachable: {succeeded}"
    finally:
        client.delete(f"/api/user/by-id/{hidden['id']}", headers=auth_headers(access_token))
        delete_admin(access_token, admin["username"])
        _drop_role(access_token, role["id"])
        delete_group(access_token, allowed["id"])
        delete_group(access_token, forbidden["id"])
        delete_core(access_token, core["id"])


def test_a_group_restricted_admin_still_reaches_users_in_its_allowed_groups(access_token):
    core = create_core(access_token)
    allowed = create_group(access_token, name=unique_name("scope_allowed"))
    role = _role(
        access_token,
        permissions={
            "users": {"create": True, "read": True, "update": True, "delete": True, "revoke_sub": True},
            "groups": {"read": True, "read_simple": True},
        },
        access={"require_template": False, "allowed_template_ids": None, "allowed_group_ids": [allowed["id"]]},
    )
    admin = create_admin(access_token, role_id=role["id"])
    token = _login(admin["username"], admin["password"])
    visible = create_user(access_token, group_ids=[allowed["id"]], payload={"username": unique_name("scope_visible")})
    try:
        listed = {u["username"] for u in client.get("/api/users", headers=auth_headers(token)).json()["users"]}
        assert visible["username"] in listed
        reached = {
            "GET by-id": client.get(f"/api/user/by-id/{visible['id']}", headers=auth_headers(token)).status_code,
            "GET by-name": client.get(f"/api/user/{visible['username']}", headers=auth_headers(token)).status_code,
            "PUT note": client.put(
                f"/api/user/by-id/{visible['id']}", headers=auth_headers(token), json={"note": "reached"}
            ).status_code,
            "revoke_sub": client.post(
                f"/api/user/by-id/{visible['id']}/revoke_sub", headers=auth_headers(token)
            ).status_code,
            "bulk disable": client.post(
                "/api/users/bulk/disable", headers=auth_headers(token), json={"ids": [visible["id"]]}
            ).status_code,
        }
        assert all(code == 200 for code in reached.values()), reached
    finally:
        client.delete(f"/api/user/by-id/{visible['id']}", headers=auth_headers(access_token))
        delete_admin(access_token, admin["username"])
        _drop_role(access_token, role["id"])
        delete_group(access_token, allowed["id"])
        delete_core(access_token, core["id"])


async def test_an_own_scope_admin_cannot_push_its_user_and_that_traffic_onto_another_admin(access_token):
    core = create_core(access_token)
    group = create_group(access_token, name=unique_name("scope_owner_group"))
    role = _role(
        access_token,
        permissions={
            "users": {
                "create": True,
                "read": {"scope": 1},
                "read_simple": {"scope": 1},
                "update": {"scope": 1},
                "set_owner": {"scope": 1},
            }
        },
    )
    reseller = create_admin(access_token, role_id=role["id"])
    victim = create_admin(access_token, role_id=role["id"])
    token = _login(reseller["username"], reseller["password"])
    created = client.post(
        "/api/user",
        headers=auth_headers(token),
        json={
            "username": unique_name("scope_heavy"),
            "proxy_settings": {},
            "data_limit": 100 * GB,
            "data_limit_reset_strategy": "no_reset",
            "status": "active",
            "group_ids": [group["id"]],
        },
    )
    assert created.status_code == 201, created.text
    user_id = created.json()["id"]
    try:
        async with TestSession() as session:
            await session.execute(update(User).where(User.id == user_id).values(used_traffic=50 * GB))
            await session.execute(
                update(Admin).where(Admin.username == reseller["username"]).values(used_traffic=50 * GB)
            )
            await session.commit()
        response = client.put(
            f"/api/user/by-id/{user_id}/set_owner",
            headers=auth_headers(token),
            params={"admin_username": victim["username"]},
        )
        async with TestSession() as session:
            rows = dict(
                (
                    await session.execute(
                        select(Admin.username, Admin.used_traffic).where(
                            Admin.username.in_([reseller["username"], victim["username"]])
                        )
                    )
                ).all()
            )
        assert response.status_code in (403, 404), (
            f"own-scope admin moved its user to another tenant (status {response.status_code}); "
            f"reseller used_traffic now {rows[reseller['username']] / GB:.0f} GB, "
            f"victim used_traffic now {rows[victim['username']] / GB:.0f} GB"
        )
    finally:
        client.delete(f"/api/user/by-id/{user_id}", headers=auth_headers(access_token))
        delete_admin(access_token, reseller["username"])
        delete_admin(access_token, victim["username"])
        _drop_role(access_token, role["id"])
        delete_group(access_token, group["id"])
        delete_core(access_token, core["id"])


def _own_scope_reseller(access_token: str, *, extra_permissions: dict | None = None) -> tuple[dict, dict, dict, str]:
    permissions = {
        "users": {
            "create": True,
            "read": {"scope": 1},
            "read_simple": {"scope": 1},
            "update": {"scope": 1},
            "set_owner": {"scope": 1},
        }
    }
    permissions.update(extra_permissions or {})
    role = _role(access_token, permissions=permissions)
    reseller = create_admin(access_token, role_id=role["id"])
    other = create_admin(access_token, role_id=role["id"])
    return role, reseller, other, _login(reseller["username"], reseller["password"])


def _reseller_user(token: str, group_id: int) -> int:
    created = client.post(
        "/api/user",
        headers=auth_headers(token),
        json={
            "username": unique_name("scope_moved"),
            "proxy_settings": {},
            "data_limit": 100 * GB,
            "data_limit_reset_strategy": "no_reset",
            "status": "active",
            "group_ids": [group_id],
        },
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


def test_an_own_scope_admin_cannot_bulk_move_its_users_onto_another_admin(access_token):
    core = create_core(access_token)
    group = create_group(access_token, name=unique_name("scope_owner_group"))
    role, reseller, other, token = _own_scope_reseller(access_token)
    user_id = _reseller_user(token, group["id"])
    try:
        response = client.put(
            "/api/users/bulk/set_owner",
            headers=auth_headers(token),
            json={"ids": [user_id], "admin_username": other["username"]},
        )
        assert response.status_code == 403, response.text
        owner = client.get(f"/api/user/by-id/{user_id}", headers=auth_headers(access_token)).json()["admin"]
        assert owner["username"] == reseller["username"]
    finally:
        client.delete(f"/api/user/by-id/{user_id}", headers=auth_headers(access_token))
        delete_admin(access_token, reseller["username"])
        delete_admin(access_token, other["username"])
        _drop_role(access_token, role["id"])
        delete_group(access_token, group["id"])
        delete_core(access_token, core["id"])


def test_an_own_scope_admin_that_may_update_admins_can_still_move_its_users(access_token):
    core = create_core(access_token)
    group = create_group(access_token, name=unique_name("scope_owner_group"))
    role, reseller, other, token = _own_scope_reseller(access_token, extra_permissions={"admins": {"update": True}})
    user_id = _reseller_user(token, group["id"])
    try:
        response = client.put(
            f"/api/user/by-id/{user_id}/set_owner",
            headers=auth_headers(token),
            params={"admin_username": other["username"]},
        )
        assert response.status_code == 200, response.text
        assert response.json()["admin"]["username"] == other["username"]
    finally:
        client.delete(f"/api/user/by-id/{user_id}", headers=auth_headers(access_token))
        delete_admin(access_token, reseller["username"])
        delete_admin(access_token, other["username"])
        _drop_role(access_token, role["id"])
        delete_group(access_token, group["id"])
        delete_core(access_token, core["id"])


async def test_a_non_owner_cannot_hand_users_to_the_owner_account(access_token):
    core = create_core(access_token)
    group = create_group(access_token, name=unique_name("scope_owner_group"))
    role = _role(access_token, permissions={"users": {"read": True, "set_owner": True}})
    manager = create_admin(access_token, role_id=role["id"])
    token = _login(manager["username"], manager["password"])
    user = create_user(access_token, group_ids=[group["id"]], payload={"username": unique_name("scope_to_owner")})
    owner_username = unique_name("scope_owner")
    async with TestSession() as session:
        session.add(Admin(username=owner_username, hashed_password="secret", role_id=1))
        await session.commit()
    try:
        single = client.put(
            f"/api/user/by-id/{user['id']}/set_owner",
            headers=auth_headers(token),
            params={"admin_username": owner_username},
        )
        bulk = client.put(
            "/api/users/bulk/set_owner",
            headers=auth_headers(token),
            json={"ids": [user["id"]], "admin_username": owner_username},
        )
        assert (single.status_code, bulk.status_code) == (403, 403), (single.text, bulk.text)
    finally:
        client.delete(f"/api/user/by-id/{user['id']}", headers=auth_headers(access_token))
        async with TestSession() as session:
            await session.execute(delete(Admin).where(Admin.username == owner_username))
            await session.commit()
        delete_admin(access_token, manager["username"])
        _drop_role(access_token, role["id"])
        delete_group(access_token, group["id"])
        delete_core(access_token, core["id"])


def test_a_next_plan_cannot_hand_out_more_than_the_role_limits_allow(access_token):
    core = create_core(access_token)
    group = create_group(access_token, name=unique_name("scope_np_group"))
    role = _role(
        access_token,
        permissions={
            "users": {
                "create": True,
                "read": {"scope": 1},
                "update": {"scope": 1},
                "activate_next_plan": {"scope": 1},
            }
        },
        limits={"data_limit_max": 10 * GB, "expire_max": 30 * 86400},
    )
    reseller = create_admin(access_token, role_id=role["id"])
    token = _login(reseller["username"], reseller["password"])
    import datetime as _dt

    expire = (_dt.datetime.now(_dt.UTC) + _dt.timedelta(days=10)).isoformat()
    created = client.post(
        "/api/user",
        headers=auth_headers(token),
        json={
            "username": unique_name("scope_np"),
            "proxy_settings": {},
            "data_limit": 10 * GB,
            "expire": expire,
            "data_limit_reset_strategy": "no_reset",
            "status": "active",
            "group_ids": [group["id"]],
            "next_plan": {"data_limit": 1024 * GB, "expire": 10 * 365 * 86400, "add_remaining_traffic": False},
        },
    )
    user_id = created.json().get("id") if created.status_code == 201 else None
    try:
        activated = None
        if user_id is not None:
            activated = client.post(f"/api/user/by-id/{user_id}/active_next", headers=auth_headers(token))
        final = activated.json() if activated is not None and activated.status_code == 200 else None
        assert final is None or final["data_limit"] <= 10 * GB, (
            f"role caps data_limit at 10 GB and expire at 30 days, yet create={created.status_code} and "
            f"active_next={activated.status_code} left data_limit={final['data_limit'] / GB:.0f} GB, "
            f"expire={final['expire']}"
        )
    finally:
        if user_id is not None:
            client.delete(f"/api/user/by-id/{user_id}", headers=auth_headers(access_token))
        delete_admin(access_token, reseller["username"])
        _drop_role(access_token, role["id"])
        delete_group(access_token, group["id"])
        delete_core(access_token, core["id"])


def test_a_next_plan_is_held_to_the_role_limits_on_create_and_modify(access_token):
    import datetime as _dt

    core = create_core(access_token)
    group = create_group(access_token, name=unique_name("scope_np_group"))
    role = _role(
        access_token,
        permissions={
            "users": {
                "create": True,
                "read": {"scope": 1},
                "update": {"scope": 1},
                "activate_next_plan": {"scope": 1},
            }
        },
        limits={"data_limit_max": 10 * GB, "expire_max": 30 * 86400},
    )
    reseller = create_admin(access_token, role_id=role["id"])
    token = _login(reseller["username"], reseller["password"])
    base = {
        "proxy_settings": {},
        "data_limit": 5 * GB,
        "expire": (_dt.datetime.now(_dt.UTC) + _dt.timedelta(days=10)).isoformat(),
        "data_limit_reset_strategy": "no_reset",
        "status": "active",
        "group_ids": [group["id"]],
    }
    rejected_plans = {
        "over data cap": {"data_limit": 11 * GB, "expire": 86400},
        "unlimited data": {"data_limit": 0, "expire": 86400},
        "over expire cap": {"data_limit": GB, "expire": 31 * 86400},
        "never expires": {"data_limit": GB, "expire": None},
        "adds remaining traffic": {"data_limit": GB, "expire": 86400, "add_remaining_traffic": True},
        "negative data": {"data_limit": -1, "expire": 86400},
    }
    created = client.post(
        "/api/user",
        headers=auth_headers(token),
        json={**base, "username": unique_name("scope_np_ok"), "next_plan": {"data_limit": 8 * GB, "expire": 86400}},
    )
    assert created.status_code == 201, created.text
    user_id = created.json()["id"]
    try:
        create_codes = {
            name: client.post(
                "/api/user",
                headers=auth_headers(token),
                json={**base, "username": unique_name("scope_np_bad"), "next_plan": plan},
            ).status_code
            for name, plan in rejected_plans.items()
        }
        modify_codes = {
            name: client.put(
                f"/api/user/by-id/{user_id}", headers=auth_headers(token), json={"next_plan": plan}
            ).status_code
            for name, plan in rejected_plans.items()
        }
        assert set(create_codes.values()) == {400}, create_codes
        assert set(modify_codes.values()) == {400}, modify_codes

        activated = client.post(f"/api/user/by-id/{user_id}/active_next", headers=auth_headers(token))
        assert activated.status_code == 200, activated.text
        assert activated.json()["data_limit"] == 8 * GB
    finally:
        client.delete(f"/api/user/by-id/{user_id}", headers=auth_headers(access_token))
        delete_admin(access_token, reseller["username"])
        _drop_role(access_token, role["id"])
        delete_group(access_token, group["id"])
        delete_core(access_token, core["id"])


def test_the_owner_cannot_store_a_negative_next_plan(access_token):
    core = create_core(access_token)
    group = create_group(access_token, name=unique_name("scope_np_group"))
    user = create_user(access_token, group_ids=[group["id"]], payload={"username": unique_name("scope_np_owner")})
    try:
        codes = {
            field: client.put(
                f"/api/user/by-id/{user['id']}",
                headers=auth_headers(access_token),
                json={"next_plan": {"data_limit": GB, "expire": 86400, field: -5}},
            ).status_code
            for field in ("data_limit", "expire")
        }
        assert codes == {"data_limit": 400, "expire": 400}, codes
        accepted = client.put(
            f"/api/user/by-id/{user['id']}",
            headers=auth_headers(access_token),
            json={"next_plan": {"data_limit": 100 * GB, "expire": 365 * 86400, "add_remaining_traffic": True}},
        )
        assert accepted.status_code == 200, accepted.text
    finally:
        client.delete(f"/api/user/by-id/{user['id']}", headers=auth_headers(access_token))
        delete_group(access_token, group["id"])
        delete_core(access_token, core["id"])
