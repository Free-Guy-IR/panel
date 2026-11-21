import asyncio

from fastapi import status
from sqlalchemy import select, update

from app.db.models import APIKey
from tests.api import TestSession, client
from tests.api.concurrent_requests import send_concurrently
from tests.api.helpers import auth_headers, create_admin, delete_admin, unique_name

INT32_MAX = 2_147_483_647


def _login(username: str, password: str) -> str:
    response = client.post(
        "/api/admin/token",
        data={"username": username, "password": password, "grant_type": "password"},
    )
    assert response.status_code == status.HTTP_200_OK
    return response.json()["access_token"]


def _create_key(admin_token: str, **fields) -> tuple[str, int]:
    response = client.post(
        "/api/api_key",
        headers=auth_headers(admin_token),
        json={"name": unique_name("api_key"), **fields},
    )
    assert response.status_code == status.HTTP_201_CREATED
    created = response.json()
    return created["api_key"], created["id"]


def _set_request_count(key_id: int, value: int) -> None:
    async def _set() -> None:
        async with TestSession() as session:
            await session.execute(update(APIKey).where(APIKey.id == key_id).values(request_count=value))
            await session.commit()

    asyncio.run(_set())


def _request_count(key_id: int) -> int | None:
    async def _get() -> int | None:
        async with TestSession() as session:
            result = await session.execute(select(APIKey.request_count).where(APIKey.id == key_id))
            return result.scalar_one_or_none()

    return asyncio.run(_get())


def test_an_unlimited_key_keeps_authenticating_at_the_request_count_ceiling(access_token):
    admin = create_admin(access_token, role_id=2)
    try:
        raw_key, key_id = _create_key(_login(admin["username"], admin["password"]))
        _set_request_count(key_id, INT32_MAX - 1)

        statuses = [client.get("/api/admin", headers={"X-Api-Key": raw_key}).status_code for _ in range(3)]

        assert statuses == [status.HTTP_200_OK] * 3
        assert _request_count(key_id) == INT32_MAX
    finally:
        delete_admin(access_token, admin["username"])


def _concurrent_admin_reads(raw_key: str, count: int) -> list[int]:
    calls = [("GET", "/api/admin", {"headers": {"X-Api-Key": raw_key}})] * count
    return [response.status_code for response in send_concurrently(calls)]


def test_concurrent_requests_of_an_unlimited_key_at_the_ceiling_all_pass(access_token):
    admin = create_admin(access_token, role_id=2)
    try:
        raw_key, key_id = _create_key(_login(admin["username"], admin["password"]))
        _set_request_count(key_id, INT32_MAX - 3)

        codes = _concurrent_admin_reads(raw_key, 12)

        assert codes == [status.HTTP_200_OK] * 12
        assert _request_count(key_id) == INT32_MAX
    finally:
        delete_admin(access_token, admin["username"])


def test_concurrent_requests_of_a_key_limited_at_the_ceiling_pass_only_up_to_its_limit(access_token):
    admin = create_admin(access_token, role_id=2)
    try:
        raw_key, key_id = _create_key(_login(admin["username"], admin["password"]), max_requests=INT32_MAX)
        _set_request_count(key_id, INT32_MAX - 3)

        codes = _concurrent_admin_reads(raw_key, 12)

        assert sorted(codes) == [status.HTTP_200_OK] * 3 + [status.HTTP_401_UNAUTHORIZED] * 9
        assert _request_count(key_id) == INT32_MAX
    finally:
        delete_admin(access_token, admin["username"])


def test_concurrent_requests_of_a_deleting_key_at_the_ceiling_delete_it_after_its_last_slot(access_token):
    admin = create_admin(access_token, role_id=2)
    try:
        raw_key, key_id = _create_key(
            _login(admin["username"], admin["password"]), max_requests=INT32_MAX, delete_on_limit=True
        )
        _set_request_count(key_id, INT32_MAX - 3)

        codes = _concurrent_admin_reads(raw_key, 12)

        assert sorted(codes) == [status.HTTP_200_OK] * 3 + [status.HTTP_401_UNAUTHORIZED] * 9
        assert _request_count(key_id) is None
    finally:
        delete_admin(access_token, admin["username"])


def test_a_key_limited_at_the_ceiling_still_stops_at_its_limit(access_token):
    admin = create_admin(access_token, role_id=2)
    try:
        raw_key, key_id = _create_key(_login(admin["username"], admin["password"]), max_requests=INT32_MAX)
        _set_request_count(key_id, INT32_MAX - 1)

        last_allowed = client.get("/api/admin", headers={"X-Api-Key": raw_key})
        assert last_allowed.status_code == status.HTTP_200_OK
        assert _request_count(key_id) == INT32_MAX

        over_the_limit = client.get("/api/admin", headers={"X-Api-Key": raw_key})
        assert over_the_limit.status_code == status.HTTP_401_UNAUTHORIZED
        assert _request_count(key_id) == INT32_MAX
    finally:
        delete_admin(access_token, admin["username"])


def test_a_key_limited_at_the_ceiling_is_deleted_by_its_last_request(access_token):
    admin = create_admin(access_token, role_id=2)
    try:
        raw_key, key_id = _create_key(
            _login(admin["username"], admin["password"]), max_requests=INT32_MAX, delete_on_limit=True
        )
        _set_request_count(key_id, INT32_MAX - 1)

        last_allowed = client.get("/api/admin", headers={"X-Api-Key": raw_key})
        assert last_allowed.status_code == status.HTTP_200_OK
        assert _request_count(key_id) is None

        after_delete = client.get("/api/admin", headers={"X-Api-Key": raw_key})
        assert after_delete.status_code == status.HTTP_401_UNAUTHORIZED
    finally:
        delete_admin(access_token, admin["username"])
