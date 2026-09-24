from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import select, update

from app.db.models import User, UserStatus, users_groups_association
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

FULL_USER_RIGHTS = {"read": True, "update": True, "delete": True, "reset_usage": True, "create": True}
GB = 1024**3


def _login(username: str, password: str) -> str:
    response = client.post(
        "/api/admin/token", data={"username": username, "password": password, "grant_type": "password"}
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


@contextmanager
def scoped_world(access_token: str):
    core = create_core(access_token)
    allowed = create_group(access_token, name=unique_name("scope_allowed"))
    forbidden = create_group(access_token, name=unique_name("scope_forbidden"))
    role = client.post(
        "/api/admin-role",
        headers=auth_headers(access_token),
        json={
            "name": unique_name("scope_role"),
            "permissions": {"users": FULL_USER_RIGHTS, "groups": {"read": True, "update": True}},
            "access": {"require_template": False, "allowed_template_ids": None, "allowed_group_ids": [allowed["id"]]},
        },
    )
    assert role.status_code == 201, role.text
    admin = create_admin(access_token, role_id=role.json()["id"])
    visible = create_user(access_token, group_ids=[allowed["id"]], payload={"username": unique_name("scope_visible")})
    hidden = create_user(access_token, group_ids=[forbidden["id"]], payload={"username": unique_name("scope_hidden")})
    world = SimpleNamespace(
        token=_login(admin["username"], admin["password"]),
        allowed=allowed,
        forbidden=forbidden,
        visible=visible,
        hidden=hidden,
        ids=[visible["id"], hidden["id"]],
    )
    try:
        yield world
    finally:
        for user in (visible, hidden):
            client.delete(f"/api/user/by-id/{user['id']}", headers=auth_headers(access_token))
        delete_admin(access_token, admin["username"])
        client.delete(f"/api/admin-role/{role.json()['id']}", headers=auth_headers(access_token))
        delete_group(access_token, allowed["id"])
        delete_group(access_token, forbidden["id"])
        delete_core(access_token, core["id"])


async def _set(user_ids: list[int], **values) -> None:
    async with TestSession() as session:
        await session.execute(update(User).where(User.id.in_(user_ids)).values(**values))
        await session.commit()


async def _column(user_ids: list[int], column) -> dict[int, object]:
    async with TestSession() as session:
        return dict((await session.execute(select(User.id, column).where(User.id.in_(user_ids)))).all())


async def _group_ids(user_id: int) -> set[int]:
    async with TestSession() as session:
        return set(
            (
                await session.execute(
                    select(users_groups_association.c.groups_id).where(users_groups_association.c.user_id == user_id)
                )
            )
            .scalars()
            .all()
        )


def _post(token: str, path: str, body: dict):
    response = client.post(path, headers=auth_headers(token), json=body)
    assert response.status_code == 200, response.text
    return response.json()


async def test_bulk_expire_by_a_group_restricted_admin_never_reaches_users_outside_its_groups(access_token):
    with scoped_world(access_token) as world:
        await _set(world.ids, expire=datetime.now(UTC).replace(microsecond=0) + timedelta(days=10))
        before = await _column(world.ids, User._expire)
        dry = _post(world.token, "/api/users/bulk/expire", {"amount": 86400, "users": world.ids, "dry_run": True})
        _post(world.token, "/api/users/bulk/expire", {"amount": 86400, "users": world.ids})
        after = await _column(world.ids, User._expire)
        assert dry["affected_users"] == 1, dry
        assert after[world.hidden["id"]] == before[world.hidden["id"]]
        assert after[world.visible["id"]] - before[world.visible["id"]] == timedelta(days=1)

        _post(access_token, "/api/users/bulk/expire", {"amount": 86400, "users": world.ids})
        owner_after = await _column(world.ids, User._expire)
        assert owner_after[world.hidden["id"]] - before[world.hidden["id"]] == timedelta(days=1)


async def test_bulk_data_limit_by_a_group_restricted_admin_never_reaches_users_outside_its_groups(access_token):
    with scoped_world(access_token) as world:
        await _set(world.ids, data_limit=10 * GB)
        dry = _post(world.token, "/api/users/bulk/data_limit", {"amount": GB, "users": world.ids, "dry_run": True})
        _post(world.token, "/api/users/bulk/data_limit", {"amount": GB, "users": world.ids})
        assert dry["affected_users"] == 1, dry
        assert await _column(world.ids, User.data_limit) == {world.visible["id"]: 11 * GB, world.hidden["id"]: 10 * GB}

        _post(access_token, "/api/users/bulk/data_limit", {"amount": GB, "users": world.ids})
        assert await _column(world.ids, User.data_limit) == {world.visible["id"]: 12 * GB, world.hidden["id"]: 11 * GB}


async def test_bulk_proxy_settings_by_a_group_restricted_admin_never_reaches_users_outside_its_groups(access_token):
    with scoped_world(access_token) as world:
        before = await _column(world.ids, User.proxy_settings)
        method = "aes-128-gcm"
        _post(world.token, "/api/users/bulk/proxy_settings", {"method": method, "users": world.ids})
        after = await _column(world.ids, User.proxy_settings)
        assert after[world.visible["id"]]["shadowsocks"]["method"] == method
        assert after[world.hidden["id"]]["shadowsocks"]["method"] == before[world.hidden["id"]]["shadowsocks"]["method"]

        _post(access_token, "/api/users/bulk/proxy_settings", {"method": method, "users": world.ids})
        assert (await _column(world.ids, User.proxy_settings))[world.hidden["id"]]["shadowsocks"]["method"] == method


async def test_entitled_secret_activation_by_a_group_restricted_admin_only_considers_its_users(
    access_token, monkeypatch
):
    from app.fork.operation import user_extras

    seen: list[list[int]] = []

    async def record_candidates(db, secret_field, *, candidate_ids, dry_run, **kwargs):
        seen.append(sorted(candidate_ids))
        return SimpleNamespace(granted=0, entitled=0, missing=0)

    monkeypatch.setattr(user_extras, "issue_missing_entitled_secrets", record_candidates)
    with scoped_world(access_token) as world:
        for path in (
            "/api/users/bulk/l2tp_activate",
            "/api/users/bulk/mtproto_activate",
            "/api/users/bulk/openvpn_activate",
        ):
            _post(world.token, path, {"users": world.ids, "dry_run": True})
        _post(access_token, "/api/users/bulk/l2tp_activate", {"users": world.ids, "dry_run": True})
    assert seen == [[world.visible["id"]]] * 3 + [sorted(world.ids)], seen


async def test_duplicate_secret_repair_by_a_group_restricted_admin_only_touches_its_users(access_token):
    with scoped_world(access_token) as world:
        settings = await _column(world.ids, User.proxy_settings)
        shared = dict(settings[world.hidden["id"]])
        shared["trojan"] = dict(settings[world.visible["id"]]["trojan"])
        await _set([world.hidden["id"]], proxy_settings=shared)
        body = {"users": world.ids, "secret_fields": ["trojan.password"], "dry_run": True}
        restricted = _post(world.token, "/api/users/bulk/repair_duplicate_secrets", body)
        owner = _post(access_token, "/api/users/bulk/repair_duplicate_secrets", body)
    assert (restricted["affected_users"], owner["affected_users"]) == (1, 2)


async def test_bulk_group_add_by_a_group_restricted_admin_cannot_pull_outside_users_into_its_scope(access_token):
    with scoped_world(access_token) as world:
        body = {"group_ids": [world.allowed["id"]], "users": [world.hidden["id"]]}
        dry = _post(world.token, "/api/groups/bulk/add", {**body, "dry_run": True})
        _post(world.token, "/api/groups/bulk/add", body)
        assert dry["affected_users"] == 0, dry
        assert await _group_ids(world.hidden["id"]) == {world.forbidden["id"]}

        _post(access_token, "/api/groups/bulk/add", body)
        assert await _group_ids(world.hidden["id"]) == {world.forbidden["id"], world.allowed["id"]}


async def test_bulk_group_remove_by_a_group_restricted_admin_only_counts_its_users(access_token):
    with scoped_world(access_token) as world:
        body = {"group_ids": [world.allowed["id"]], "users": world.ids, "dry_run": True}
        restricted = _post(world.token, "/api/groups/bulk/remove", body)
        owner = _post(access_token, "/api/groups/bulk/remove", body)
    assert (restricted["affected_users"], owner["affected_users"]) == (1, 2)


async def test_reset_all_usage_by_a_group_restricted_admin_only_resets_its_users(access_token):
    with scoped_world(access_token) as world:
        await _set(world.ids, used_traffic=5 * GB)
        response = client.post("/api/users/reset", headers=auth_headers(world.token))
        assert response.status_code == 200, response.text
        assert await _column(world.ids, User.used_traffic) == {world.visible["id"]: 0, world.hidden["id"]: 5 * GB}

        response = client.post("/api/users/reset", headers=auth_headers(access_token))
        assert response.status_code == 200, response.text
        assert await _column(world.ids, User.used_traffic) == {world.visible["id"]: 0, world.hidden["id"]: 0}


async def test_expired_cleanup_by_a_group_restricted_admin_never_lists_or_deletes_users_outside_its_groups(
    access_token,
):
    with scoped_world(access_token) as world:
        long_ago = datetime.now(UTC) - timedelta(days=40)
        await _set(world.ids, status=UserStatus.limited, last_status_change=long_ago)
        params = {"target": "limited"}
        listed = client.get("/api/users/expired", headers=auth_headers(world.token), params=params)
        assert listed.status_code == 200, listed.text
        owner_listed = client.get("/api/users/expired", headers=auth_headers(access_token), params=params)
        assert owner_listed.status_code == 200, owner_listed.text
        deleted = client.delete("/api/users/expired", headers=auth_headers(world.token), params=params)
        assert deleted.status_code == 200, deleted.text
        remaining = set(await _column(world.ids, User.id))

        assert world.hidden["username"] not in listed.json()
        assert world.visible["username"] in listed.json()
        assert {world.hidden["username"], world.visible["username"]} <= set(owner_listed.json())
        assert deleted.json()["users"] == [world.visible["username"]]
        assert remaining == {world.hidden["id"]}
