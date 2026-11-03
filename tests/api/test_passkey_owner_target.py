import asyncio
import secrets

import pytest
from fastapi import status
from sqlalchemy import select

from app.db.models import Admin, AdminPasskey
from app.models.admin import hash_password
from tests.api import TestSession, client
from tests.api.helpers import auth_headers, create_admin, delete_admin, strong_password, unique_name


def _login(username: str, password: str) -> str:
    response = client.post(
        "/api/admin/token",
        data={"username": username, "password": password, "grant_type": "password"},
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    return response.json()["access_token"]


def _create_owner() -> dict:
    username = unique_name("pk_owner")
    password = strong_password("PkOwner")

    async def _create() -> int:
        async with TestSession() as session:
            db_admin = Admin(username=username, hashed_password=await hash_password(password), role_id=1)
            session.add(db_admin)
            await session.commit()
            await session.refresh(db_admin)
            return db_admin.id

    return {"id": asyncio.run(_create()), "username": username, "password": password}


def _delete_admin_row(username: str) -> None:
    async def _delete() -> None:
        async with TestSession() as session:
            db_admin = (await session.execute(select(Admin).where(Admin.username == username))).scalar_one_or_none()
            if db_admin is not None:
                await session.delete(db_admin)
                await session.commit()

    asyncio.run(_delete())


def _add_passkey(admin_id: int) -> int:
    async def _add() -> int:
        async with TestSession() as session:
            row = AdminPasskey(
                admin_id=admin_id,
                credential_id=secrets.token_bytes(32),
                public_key=secrets.token_bytes(64),
                sign_count=0,
                name="device",
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row.id

    return asyncio.run(_add())


def _passkey_exists(passkey_id: int) -> bool:
    async def _get() -> bool:
        async with TestSession() as session:
            row = (
                await session.execute(select(AdminPasskey).where(AdminPasskey.id == passkey_id))
            ).scalar_one_or_none()
            return row is not None

    return asyncio.run(_get())


@pytest.fixture
def passkey_manager(access_token):
    role = client.post(
        "/api/admin-role",
        headers=auth_headers(access_token),
        json={
            "name": unique_name("pk_manager"),
            "permissions": {"admins": {"read": True, "passkeys": True}},
            "limits": {},
            "features": {},
            "access": {},
        },
    )
    assert role.status_code == status.HTTP_201_CREATED, role.text
    role_id = role.json()["id"]
    manager = create_admin(access_token, role_id=role_id)
    owner = _create_owner()
    peer = create_admin(access_token, role_id=3)
    try:
        yield {
            "headers": auth_headers(_login(manager["username"], manager["password"])),
            "owner": owner,
            "peer": peer,
        }
    finally:
        _delete_admin_row(owner["username"])
        delete_admin(access_token, peer["username"])
        delete_admin(access_token, manager["username"])
        client.delete(f"/api/admin-role/{role_id}", headers=auth_headers(access_token))


def test_a_non_owner_cannot_list_the_owner_passkeys(passkey_manager):
    _add_passkey(passkey_manager["owner"]["id"])

    response = client.get(
        f"/api/admin/passkey/admins/{passkey_manager['owner']['id']}", headers=passkey_manager["headers"]
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN, response.text


def test_a_non_owner_cannot_delete_an_owner_passkey(passkey_manager):
    passkey_id = _add_passkey(passkey_manager["owner"]["id"])

    response = client.delete(
        f"/api/admin/passkey/admins/{passkey_manager['owner']['id']}/{passkey_id}", headers=passkey_manager["headers"]
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN, response.text
    assert _passkey_exists(passkey_id)


def test_a_passkey_manager_still_manages_a_non_owner_admin(passkey_manager):
    passkey_id = _add_passkey(passkey_manager["peer"]["id"])
    path = f"/api/admin/passkey/admins/{passkey_manager['peer']['id']}"

    listed = client.get(path, headers=passkey_manager["headers"])
    assert listed.status_code == status.HTTP_200_OK, listed.text
    assert [row["id"] for row in listed.json()] == [passkey_id]

    deleted = client.delete(f"{path}/{passkey_id}", headers=passkey_manager["headers"])
    assert deleted.status_code == status.HTTP_200_OK, deleted.text
    assert not _passkey_exists(passkey_id)


def test_the_owner_still_manages_its_own_passkeys(passkey_manager):
    owner = passkey_manager["owner"]
    passkey_id = _add_passkey(owner["id"])
    headers = auth_headers(_login(owner["username"], owner["password"]))

    listed = client.get(f"/api/admin/passkey/admins/{owner['id']}", headers=headers)
    assert listed.status_code == status.HTTP_200_OK, listed.text

    deleted = client.delete(f"/api/admin/passkey/admins/{owner['id']}/{passkey_id}", headers=headers)
    assert deleted.status_code == status.HTTP_200_OK, deleted.text


def test_the_env_sudoer_still_manages_owner_passkeys(access_token, passkey_manager):
    owner = passkey_manager["owner"]
    passkey_id = _add_passkey(owner["id"])

    listed = client.get(f"/api/admin/passkey/admins/{owner['id']}", headers=auth_headers(access_token))
    assert listed.status_code == status.HTTP_200_OK, listed.text

    deleted = client.delete(f"/api/admin/passkey/admins/{owner['id']}/{passkey_id}", headers=auth_headers(access_token))
    assert deleted.status_code == status.HTTP_200_OK, deleted.text


def test_an_unprivileged_admin_cannot_tell_the_owner_from_a_peer(access_token, passkey_manager):
    plain = create_admin(access_token, role_id=3)
    try:
        headers = auth_headers(_login(plain["username"], plain["password"]))
        owner = client.get(f"/api/admin/passkey/admins/{passkey_manager['owner']['id']}", headers=headers)
        peer = client.get(f"/api/admin/passkey/admins/{passkey_manager['peer']['id']}", headers=headers)
        assert owner.status_code == peer.status_code == status.HTTP_403_FORBIDDEN
        assert owner.json() == peer.json()
    finally:
        delete_admin(access_token, plain["username"])
