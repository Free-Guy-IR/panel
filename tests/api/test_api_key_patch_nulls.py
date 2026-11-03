import asyncio

import pytest
from fastapi import status
from sqlalchemy import literal_column, select, update

from app.db.models import APIKey
from tests.api import TestSession, client
from tests.api.helpers import auth_headers, create_admin, delete_admin, unique_name

SCOPE_OWN = {"scope": 1}


def _login(username: str, password: str) -> str:
    response = client.post(
        "/api/admin/token",
        data={"username": username, "password": password, "grant_type": "password"},
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    return response.json()["access_token"]


@pytest.fixture
def key_admin(access_token):
    role = client.post(
        "/api/admin-role",
        headers=auth_headers(access_token),
        json={
            "name": unique_name("null_patch_role"),
            "permissions": {
                "users": {"read": SCOPE_OWN},
                "api_keys": {"create": True, "read": True, "update": True, "delete": True},
            },
            "limits": {},
            "features": {},
            "access": {},
        },
    )
    assert role.status_code == status.HTTP_201_CREATED, role.text
    role_id = role.json()["id"]
    actor = create_admin(access_token, role_id=role_id)
    try:
        yield auth_headers(_login(actor["username"], actor["password"]))
    finally:
        delete_admin(access_token, actor["username"])
        client.delete(f"/api/admin-role/{role_id}", headers=auth_headers(access_token))


def _create_custom_key(headers: dict[str, str]) -> dict:
    response = client.post(
        "/api/api_key",
        headers=headers,
        json={
            "name": unique_name("null_key"),
            "inherit_permissions": False,
            "permissions": {"users": {"read": SCOPE_OWN}},
        },
    )
    assert response.status_code == status.HTTP_201_CREATED, response.text
    return response.json()


def _store_json_null_permissions(key_id: int) -> None:
    async def _write() -> None:
        async with TestSession() as session:
            await session.execute(
                update(APIKey).where(APIKey.id == key_id).values(permissions=literal_column("'null'"))
            )
            await session.commit()

    asyncio.run(_write())


def _stored_row(key_id: int) -> tuple[bool, dict | None, str, str]:
    async def _read():
        async with TestSession() as session:
            row = (await session.execute(select(APIKey).where(APIKey.id == key_id))).scalar_one()
            return row.inherit_permissions, row.permissions, row.name, row.status.value

    return asyncio.run(_read())


def test_a_legacy_json_null_custom_key_fails_closed_without_a_server_error(key_admin):
    key = _create_custom_key(key_admin)
    _store_json_null_permissions(key["id"])
    key_headers = {"X-Api-Key": key["api_key"]}

    switched = client.patch(f"/api/api_key/{key['id']}", headers=key_admin, json={"inherit_permissions": False})
    users = client.get("/api/users", headers=key_headers)
    rotated = client.post(f"/api/api_key/{key['id']}/revoke", headers=key_admin)

    assert switched.status_code == status.HTTP_200_OK, switched.text
    assert users.status_code == status.HTTP_403_FORBIDDEN, users.text
    assert rotated.status_code == status.HTTP_200_OK, rotated.text
    assert client.get("/api/users", headers={"X-Api-Key": rotated.json()["api_key"]}).status_code == 403
    assert _stored_row(key["id"])[0] is False


@pytest.mark.parametrize("field", ["inherit_permissions", "name", "status"])
def test_an_explicit_null_on_a_required_field_is_rejected_as_invalid(key_admin, field):
    key = _create_custom_key(key_admin)
    before = _stored_row(key["id"])

    response = client.patch(f"/api/api_key/{key['id']}", headers=key_admin, json={field: None})

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, response.text
    assert _stored_row(key["id"]) == before
    assert client.get("/api/users", headers={"X-Api-Key": key["api_key"]}).status_code == status.HTTP_200_OK


def test_the_update_schema_documents_the_required_fields_as_non_nullable_without_a_default():
    schema = client.app.openapi()["components"]["schemas"]["APIKeyUpdate"]["properties"]

    assert schema["status"] == {"$ref": "#/components/schemas/APIKeyStatus"}
    assert schema["name"] == {"type": "string", "minLength": 1, "maxLength": 128, "title": "Name"}
    assert schema["inherit_permissions"] == {"type": "boolean", "title": "Inherit Permissions"}


@pytest.mark.parametrize(
    "payload,location",
    [
        ({"name": ""}, "name"),
        ({"status": "bogus"}, "status"),
        ({"inherit_permissions": "x"}, "inherit_permissions"),
    ],
)
def test_an_invalid_value_on_a_required_field_is_reported_at_the_field(key_admin, payload, location):
    key = _create_custom_key(key_admin)

    response = client.patch(f"/api/api_key/{key['id']}", headers=key_admin, json=payload)

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, response.text
    assert list(response.json()["detail"]) == [location]
