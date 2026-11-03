import asyncio

import pytest
from fastapi import status
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.crud.admin_role import get_role, modify_role
from app.models.admin_role import AdminRoleModify, PermissionScope, RolePermissions
from app.models.api_key import APIKeyUpdate
from app.operation import OperatorType
from app.operation.api_key import APIKeyOperation
from app.operation.permissions import _get_resource_action
from app.routers.authentication import get_admin, get_admin_from_api_key
from tests.api import DATABASE_URL, IS_SQLITE, client
from tests.api.helpers import auth_headers, create_admin, create_user, delete_admin, unique_name

SCOPE_OWN = {"scope": 1}
SCOPE_ALL = {"scope": 2}
WIDE = {
    "users": {"read": SCOPE_ALL, "read_simple": SCOPE_ALL},
    "api_keys": {"create": True, "read": True, "update": True},
}
NARROW = {
    "users": {"read": SCOPE_OWN, "read_simple": SCOPE_OWN},
    "api_keys": {"create": True, "read": True, "update": True},
}


def _login(username: str, password: str) -> str:
    response = client.post(
        "/api/admin/token",
        data={"username": username, "password": password, "grant_type": "password"},
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    return response.json()["access_token"]


def _set_role_permissions(access_token: str, role_id: int, permissions: dict) -> None:
    response = client.put(
        f"/api/admin-role/{role_id}", headers=auth_headers(access_token), json={"permissions": permissions}
    )
    assert response.status_code == status.HTTP_200_OK, response.text


@pytest.fixture
def wide_operator(access_token):
    role = client.post(
        "/api/admin-role",
        headers=auth_headers(access_token),
        json={"name": unique_name("ceiling_role"), "permissions": WIDE, "limits": {}, "features": {}, "access": {}},
    )
    assert role.status_code == status.HTTP_201_CREATED, role.text
    role_id = role.json()["id"]
    actor = create_admin(access_token, role_id=role_id)
    victim = create_user(access_token)
    try:
        yield {
            "role_id": role_id,
            "actor": actor,
            "token": _login(actor["username"], actor["password"]),
            "victim": victim,
        }
    finally:
        client.delete(f"/api/user/by-id/{victim['id']}", headers=auth_headers(access_token))
        delete_admin(access_token, actor["username"])
        client.delete(f"/api/admin-role/{role_id}", headers=auth_headers(access_token))


def _create_custom_key(token: str, permissions: dict) -> tuple[int, str]:
    response = client.post(
        "/api/api_key",
        headers=auth_headers(token),
        json={"name": unique_name("ceiling_key"), "inherit_permissions": False, "permissions": permissions},
    )
    assert response.status_code == status.HTTP_201_CREATED, response.text
    return response.json()["id"], response.json()["api_key"]


def _victim_visible(raw_key: str, victim: dict) -> bool:
    response = client.get("/api/users", headers={"X-Api-Key": raw_key}, params={"username": victim["username"]})
    if response.status_code == status.HTTP_403_FORBIDDEN:
        return False
    assert response.status_code == status.HTTP_200_OK, response.text
    return victim["username"] in {user["username"] for user in response.json()["users"]}


def test_a_custom_key_loses_what_the_admin_role_no_longer_grants(access_token, wide_operator):
    _, raw_key = _create_custom_key(wide_operator["token"], {"users": {"read": SCOPE_ALL}})
    assert _victim_visible(raw_key, wide_operator["victim"])

    _set_role_permissions(access_token, wide_operator["role_id"], NARROW)

    assert not _victim_visible(raw_key, wide_operator["victim"])
    assert client.get("/api/users", headers={"X-Api-Key": raw_key}).status_code == status.HTTP_200_OK


def test_a_custom_key_is_denied_an_action_the_role_drops_entirely(access_token, wide_operator):
    _, raw_key = _create_custom_key(wide_operator["token"], {"users": {"read": SCOPE_ALL}})

    _set_role_permissions(access_token, wide_operator["role_id"], {"api_keys": NARROW["api_keys"]})

    assert client.get("/api/users", headers={"X-Api-Key": raw_key}).status_code == status.HTTP_403_FORBIDDEN


def test_a_custom_key_follows_a_move_to_a_narrower_role(access_token, wide_operator):
    _, raw_key = _create_custom_key(wide_operator["token"], {"users": {"read": SCOPE_ALL}})

    response = client.put(
        f"/api/admin/{wide_operator['actor']['username']}",
        headers=auth_headers(access_token),
        json={"role_id": 3},
    )
    assert response.status_code == status.HTTP_200_OK, response.text

    assert not _victim_visible(raw_key, wide_operator["victim"])


def test_a_custom_key_keeps_what_the_role_still_grants(access_token, wide_operator):
    _, raw_key = _create_custom_key(wide_operator["token"], {"users": {"read": SCOPE_OWN}})

    _set_role_permissions(access_token, wide_operator["role_id"], WIDE)

    assert client.get("/api/users", headers={"X-Api-Key": raw_key}).status_code == status.HTTP_200_OK
    assert not _victim_visible(raw_key, wide_operator["victim"])


def test_a_patch_revalidates_stored_permissions_against_the_current_role(access_token, wide_operator):
    key_id, _ = _create_custom_key(wide_operator["token"], {"users": {"read": SCOPE_ALL}})
    _set_role_permissions(access_token, wide_operator["role_id"], NARROW)

    response = client.patch(
        f"/api/api_key/{key_id}", headers=auth_headers(wide_operator["token"]), json={"inherit_permissions": False}
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN, response.text


def _level(value) -> PermissionScope:
    if value is True:
        return PermissionScope.ALL
    if isinstance(value, dict) and set(value) == {"scope"}:
        return PermissionScope(value["scope"])
    return PermissionScope.NONE


def _own_connection_sessions():
    connect_args = {"timeout": 30} if IS_SQLITE else {}
    engine = create_async_engine(DATABASE_URL, poolclass=NullPool, connect_args=connect_args)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.skipif(":memory:" in DATABASE_URL, reason="needs a database shared by several connections")
@pytest.mark.parametrize("with_metrics", [False, True])
def test_role_narrowing_caps_a_key_patched_concurrently_with_stale_admin_details(wide_operator, with_metrics):
    key_id, raw_key = _create_custom_key(wide_operator["token"], {"users": {"read": SCOPE_OWN}})
    role_id = wide_operator["role_id"]
    actor_token = wide_operator["token"]
    operation = APIKeyOperation(operator_type=OperatorType.API)

    async def _scenario() -> tuple[list, list]:
        engine, sessions = _own_connection_sessions()
        narrowed = asyncio.Event()
        first_patch = asyncio.Event()
        writers_done = asyncio.Event()
        async with sessions() as db:
            stale_actor = await get_admin(db, actor_token)
            await db.rollback()
        assert stale_actor is not None

        async def _reader() -> list:
            seen = []
            reads_after_narrowing = 0
            for _ in range(5000):
                if reads_after_narrowing >= 10 and writers_done.is_set():
                    break
                was_narrowed = narrowed.is_set()
                async with sessions() as db:
                    admin = await get_admin_from_api_key(db, raw_key, with_metrics=with_metrics)
                    await db.rollback()
                level = _level(_get_resource_action(admin, "users", "read")) if admin else PermissionScope.NONE
                seen.append((was_narrowed, level))
                reads_after_narrowing += was_narrowed
                await asyncio.sleep(0.001)
            return seen

        async def _narrow_role() -> None:
            await first_patch.wait()
            await asyncio.sleep(0.002)
            async with sessions() as db:
                role = await get_role(db, role_id)
                await modify_role(db, role, AdminRoleModify(permissions=RolePermissions.model_validate(NARROW)))
                await db.commit()
            narrowed.set()

        async def _patch_key() -> list:
            outcomes = []
            for step in range(6):
                await asyncio.sleep(0.002)
                payload = (
                    {"inherit_permissions": False, "permissions": {"users": {"read": SCOPE_ALL}}}
                    if step % 2 == 0
                    else {"permissions": {"users": {"read": SCOPE_ALL, "read_simple": SCOPE_ALL}}}
                )
                async with sessions() as db:
                    try:
                        await operation.modify_api_key(
                            db, admin=stale_actor, key_id=key_id, model=APIKeyUpdate.model_validate(payload)
                        )
                        outcomes.append("ok")
                    except Exception as exc:
                        await db.rollback()
                        outcomes.append(type(exc).__name__)
                first_patch.set()
            return outcomes

        async def _writers() -> list:
            try:
                results = await asyncio.gather(_narrow_role(), _patch_key())
            finally:
                writers_done.set()
            return results[1]

        try:
            results = await asyncio.gather(_reader(), _reader(), _writers())
        finally:
            await engine.dispose()
        return [entry for reader in results[:2] for entry in reader], results[2]

    seen, patch_outcomes = asyncio.run(_scenario())

    assert patch_outcomes[0] == "ok", patch_outcomes
    after = [level for was_narrowed, level in seen if was_narrowed]
    assert after
    assert all(level <= PermissionScope.OWN for level in after), (after, patch_outcomes)
    assert not _victim_visible(raw_key, wide_operator["victim"])
