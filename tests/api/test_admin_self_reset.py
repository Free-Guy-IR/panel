import asyncio

import pytest
from fastapi import HTTPException, status
from sqlalchemy import select, update

from app.db.models import Admin, AdminStatus
from app.models.admin import AdminModify, BulkAdminSelection, hash_password
from app.operation import OperatorType
from app.operation.admin import AdminOperation
from app.routers.authentication import get_admin
from tests.api import TestSession, client
from tests.api.helpers import auth_headers, create_admin, delete_admin, strong_password, unique_name

GIB = 1024**3
SELF_RESET_REFUSAL = "You're not allowed to reset your own usage."
SELF_ENABLE_REFUSAL = "You're not allowed to enable your own account."
MANAGER_PERMISSIONS = {"admins": {"read": True, "update": True, "reset_usage": True}}
ROLE_PERMISSIONS = {**MANAGER_PERMISSIONS, "api_keys": {"read": True, "create": True, "delete": True}}


def _role(access_token: str) -> int:
    response = client.post(
        "/api/admin-role",
        headers=auth_headers(access_token),
        json={
            "name": unique_name("role_self_reset"),
            "permissions": ROLE_PERMISSIONS,
            "limits": {},
            "features": {},
            "access": {},
        },
    )
    assert response.status_code == status.HTTP_201_CREATED, response.text
    return response.json()["id"]


def _login(username: str, password: str) -> str:
    response = client.post(
        "/api/admin/token",
        data={"username": username, "password": password, "grant_type": "password"},
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    return response.json()["access_token"]


def _set_state(admin_id: int, *, used_traffic: int, data_limit: int | None, admin_status: AdminStatus) -> None:
    async def _write() -> None:
        async with TestSession() as session:
            await session.execute(
                update(Admin)
                .where(Admin.id == admin_id)
                .values(used_traffic=used_traffic, data_limit=data_limit, status=admin_status)
            )
            await session.commit()

    asyncio.run(_write())


def _state(admin_id: int) -> tuple[int, AdminStatus]:
    async def _read() -> tuple[int, AdminStatus]:
        async with TestSession() as session:
            row = (await session.execute(select(Admin.used_traffic, Admin.status).where(Admin.id == admin_id))).one()
            return int(row.used_traffic or 0), row.status

    return asyncio.run(_read())


def _limit(admin_id: int) -> None:
    _set_state(admin_id, used_traffic=20 * GIB, data_limit=10 * GIB, admin_status=AdminStatus.limited)


def _over_quota_but_still_active(admin_id: int) -> None:
    _set_state(admin_id, used_traffic=20 * GIB, data_limit=10 * GIB, admin_status=AdminStatus.active)


def _refusal_with_stale_session(token: str, call) -> HTTPException | None:
    async def _run() -> HTTPException | None:
        async with TestSession() as db:
            caller = await get_admin(db, token)
            await db.rollback()
        return caller

    caller = asyncio.run(_run())
    assert caller is not None and not caller.is_limited

    async def _act() -> HTTPException | None:
        async with TestSession() as db:
            try:
                await call(AdminOperation(operator_type=OperatorType.API), db, caller)
            except HTTPException as exc:
                return exc
        return None

    return _act


@pytest.fixture
def managers(access_token):
    role_id = _role(access_token)
    me = create_admin(access_token, role_id=role_id)
    other = create_admin(access_token, role_id=role_id)
    token = _login(me["username"], me["password"])
    try:
        yield {"me": me, "other": other, "token": token, "role_id": role_id}
    finally:
        delete_admin(access_token, me["username"])
        delete_admin(access_token, other["username"])
        client.delete(f"/api/admin-role/{role_id}", headers=auth_headers(access_token))


def _assert_refused(response, message: str) -> None:
    assert response.status_code == status.HTTP_403_FORBIDDEN, response.text
    assert response.json()["detail"] == message


@pytest.mark.parametrize(
    "path",
    ["/api/admin/{username}/reset", "/api/admin/by-username/{username}/reset", "/api/admin/by-id/{id}/reset"],
)
def test_an_over_quota_admin_cannot_reset_its_own_usage(managers, path):
    me = managers["me"]
    _over_quota_but_still_active(me["id"])

    response = client.post(path.format(username=me["username"], id=me["id"]), headers=auth_headers(managers["token"]))

    _assert_refused(response, SELF_RESET_REFUSAL)
    assert _state(me["id"]) == (20 * GIB, AdminStatus.active)


def test_a_bulk_reset_of_only_itself_is_refused(managers):
    me = managers["me"]
    _over_quota_but_still_active(me["id"])

    response = client.post("/api/admins/bulk/reset", headers=auth_headers(managers["token"]), json={"ids": [me["id"]]})

    _assert_refused(response, SELF_RESET_REFUSAL)
    assert _state(me["id"]) == (20 * GIB, AdminStatus.active)


def test_a_bulk_reset_including_itself_resets_nobody(managers):
    me, other = managers["me"], managers["other"]
    _over_quota_but_still_active(me["id"])
    _limit(other["id"])

    response = client.post(
        "/api/admins/bulk/reset",
        headers=auth_headers(managers["token"]),
        json={"ids": [other["id"], me["id"]]},
    )

    _assert_refused(response, SELF_RESET_REFUSAL)
    assert _state(me["id"]) == (20 * GIB, AdminStatus.active)
    assert _state(other["id"]) == (20 * GIB, AdminStatus.limited)


def test_a_limited_admin_is_refused_bulk_enable_at_the_gate(managers):
    me, other = managers["me"], managers["other"]
    _limit(me["id"])
    _set_state(other["id"], used_traffic=0, data_limit=None, admin_status=AdminStatus.disabled)

    response = client.post(
        "/api/admins/bulk/enable",
        headers=auth_headers(managers["token"]),
        json={"ids": [other["id"], me["id"]]},
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN, response.text
    assert _state(me["id"]) == (20 * GIB, AdminStatus.limited)
    assert _state(other["id"]) == (0, AdminStatus.disabled)


def test_an_admin_limited_after_authenticating_cannot_bulk_enable_itself(managers):
    me, other = managers["me"], managers["other"]
    _set_state(other["id"], used_traffic=0, data_limit=None, admin_status=AdminStatus.disabled)

    async def _enable(operation, db, caller):
        selection = BulkAdminSelection(ids={other["id"], me["id"]})
        await operation.bulk_set_admins_disabled(db, selection, caller, is_disabled=False)

    act = _refusal_with_stale_session(managers["token"], _enable)
    _limit(me["id"])
    refusal = asyncio.run(act())

    assert refusal is not None and refusal.status_code == status.HTTP_403_FORBIDDEN, refusal
    assert refusal.detail == SELF_ENABLE_REFUSAL
    assert _state(me["id"]) == (20 * GIB, AdminStatus.limited)
    assert _state(other["id"]) == (0, AdminStatus.disabled)


def test_an_admin_limited_after_authenticating_cannot_edit_itself_back_to_active(managers):
    me = managers["me"]

    async def _edit(operation, db, caller):
        await operation.modify_admin_by_id(db, me["id"], AdminModify(status="active"), caller)

    act = _refusal_with_stale_session(managers["token"], _edit)
    _limit(me["id"])
    refusal = asyncio.run(act())

    assert refusal is not None and refusal.status_code == status.HTTP_403_FORBIDDEN, refusal
    assert _state(me["id"]) == (20 * GIB, AdminStatus.limited)


@pytest.mark.parametrize("bulk", [False, True])
def test_an_admin_limited_after_authenticating_cannot_reset_itself(managers, bulk):
    me, other = managers["me"], managers["other"]
    _limit(other["id"])

    async def _reset(operation, db, caller):
        if bulk:
            await operation.bulk_reset_admins_usage(db, BulkAdminSelection(ids={other["id"], me["id"]}), caller)
        else:
            await operation.reset_admin_usage_by_id(db, me["id"], caller)

    act = _refusal_with_stale_session(managers["token"], _reset)
    _limit(me["id"])
    refusal = asyncio.run(act())

    assert refusal is not None and refusal.status_code == status.HTTP_403_FORBIDDEN, refusal
    assert refusal.detail == SELF_RESET_REFUSAL
    assert _state(me["id"]) == (20 * GIB, AdminStatus.limited)
    assert _state(other["id"]) == (20 * GIB, AdminStatus.limited)


def test_a_bulk_enable_with_an_already_active_self_is_allowed(managers):
    me, other = managers["me"], managers["other"]
    _set_state(other["id"], used_traffic=0, data_limit=None, admin_status=AdminStatus.disabled)

    response = client.post(
        "/api/admins/bulk/enable",
        headers=auth_headers(managers["token"]),
        json={"ids": [other["id"], me["id"]]},
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["admins"] == [other["username"]]
    assert _state(me["id"])[1] == AdminStatus.active
    assert _state(other["id"])[1] == AdminStatus.active


def test_a_non_owner_can_still_reset_and_enable_another_admin(managers):
    other = managers["other"]
    _limit(other["id"])

    response = client.post(f"/api/admin/by-id/{other['id']}/reset", headers=auth_headers(managers["token"]))
    assert response.status_code == status.HTTP_200_OK, response.text
    assert _state(other["id"]) == (0, AdminStatus.active)

    _limit(other["id"])
    response = client.post(
        "/api/admins/bulk/reset", headers=auth_headers(managers["token"]), json={"ids": [other["id"]]}
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    assert _state(other["id"]) == (0, AdminStatus.active)

    _limit(other["id"])
    response = client.post(
        "/api/admins/bulk/enable", headers=auth_headers(managers["token"]), json={"ids": [other["id"]]}
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    assert _state(other["id"])[1] == AdminStatus.active


def test_the_env_sudoer_can_reset_and_enable_anyone(access_token, managers):
    me = managers["me"]
    _limit(me["id"])

    response = client.post("/api/admins/bulk/reset", headers=auth_headers(access_token), json={"ids": [me["id"]]})
    assert response.status_code == status.HTTP_200_OK, response.text
    assert _state(me["id"]) == (0, AdminStatus.active)

    _limit(me["id"])
    response = client.post("/api/admins/bulk/enable", headers=auth_headers(access_token), json={"ids": [me["id"]]})
    assert response.status_code == status.HTTP_200_OK, response.text
    assert _state(me["id"])[1] == AdminStatus.active


@pytest.fixture
def db_owner():
    username = unique_name("self_reset_owner")
    password = strong_password("SelfResetOwner")

    async def _insert() -> int:
        async with TestSession() as session:
            db_admin = Admin(username=username, hashed_password=await hash_password(password), role_id=1)
            session.add(db_admin)
            await session.commit()
            await session.refresh(db_admin)
            return db_admin.id

    async def _drop() -> None:
        async with TestSession() as session:
            db_admin = (await session.execute(select(Admin).where(Admin.username == username))).scalar_one_or_none()
            if db_admin is not None:
                await session.delete(db_admin)
                await session.commit()

    owner_id = asyncio.run(_insert())
    try:
        yield {"id": owner_id, "username": username, "token": _login(username, password)}
    finally:
        asyncio.run(_drop())


def test_an_owner_can_reset_its_own_usage(db_owner):
    _set_state(db_owner["id"], used_traffic=20 * GIB, data_limit=None, admin_status=AdminStatus.active)

    response = client.post(f"/api/admin/by-id/{db_owner['id']}/reset", headers=auth_headers(db_owner["token"]))
    assert response.status_code == status.HTTP_200_OK, response.text
    assert _state(db_owner["id"])[0] == 0

    _set_state(db_owner["id"], used_traffic=20 * GIB, data_limit=None, admin_status=AdminStatus.active)
    response = client.post(
        "/api/admins/bulk/reset", headers=auth_headers(db_owner["token"]), json={"ids": [db_owner["id"]]}
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    assert _state(db_owner["id"])[0] == 0


def test_the_admins_own_custom_api_key_cannot_reset_its_own_usage(managers):
    me = managers["me"]
    created = client.post(
        "/api/api_key",
        headers=auth_headers(managers["token"]),
        json={"name": unique_name("self_reset_key"), "inherit_permissions": False, "permissions": MANAGER_PERMISSIONS},
    )
    assert created.status_code == status.HTTP_201_CREATED, created.text
    key = created.json()
    key_headers = {"X-Api-Key": key["api_key"]}
    _over_quota_but_still_active(me["id"])
    try:
        response = client.post(f"/api/admin/by-id/{me['id']}/reset", headers=key_headers)
        _assert_refused(response, SELF_RESET_REFUSAL)

        response = client.post("/api/admins/bulk/reset", headers=key_headers, json={"ids": [me["id"]]})
        _assert_refused(response, SELF_RESET_REFUSAL)

        assert _state(me["id"]) == (20 * GIB, AdminStatus.active)

        response = client.post(f"/api/admin/by-id/{managers['other']['id']}/reset", headers=key_headers)
        assert response.status_code == status.HTTP_200_OK, response.text
    finally:
        client.delete(f"/api/api_key/{key['id']}", headers=auth_headers(managers["token"]))
