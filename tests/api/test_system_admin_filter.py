import asyncio
from contextlib import contextmanager

import pytest
from fastapi import status
from pytest import MonkeyPatch

from app.db.models import Admin, AdminRole
from app.models.admin import hash_password as _hash_password
from app.utils.system import CPUStat, DiskStat, MemoryStat
from tests.api import TestSession, client
from tests.api.helpers import auth_headers, create_admin, delete_admin, strong_password, unique_name

STATS_ENDPOINTS = ("/api/system", "/api/system/users")


@pytest.fixture
def fake_resources(monkeypatch: MonkeyPatch):
    monkeypatch.setattr("app.operation.system.memory_usage", lambda: MemoryStat(total=16_000, used=8_000, free=8_000))
    monkeypatch.setattr("app.operation.system.cpu_usage", lambda: CPUStat(cores=8, percent=42.5))
    monkeypatch.setattr("app.operation.system.disk_usage", lambda: DiskStat(total=100_000, used=40_000, free=60_000))
    monkeypatch.setattr("app.operation.system.get_uptime", lambda: 123)


@contextmanager
def role_admin(permissions: dict):
    username = unique_name("stats_filter_admin")
    password = strong_password("StatsFilter")
    role_name = unique_name("stats_filter_role")

    async def _create() -> tuple[int, int]:
        async with TestSession() as session:
            role = AdminRole(name=role_name, permissions=permissions, limits={}, features={}, access={}, hwid={})
            session.add(role)
            await session.flush()
            admin = Admin(username=username, hashed_password=await _hash_password(password), role_id=role.id)
            session.add(admin)
            await session.commit()
            return role.id, admin.id

    async def _cleanup(role_id: int, admin_id: int) -> None:
        async with TestSession() as session:
            admin = await session.get(Admin, admin_id)
            if admin:
                await session.delete(admin)
            role = await session.get(AdminRole, role_id)
            if role:
                await session.delete(role)
            await session.commit()

    role_id, admin_id = asyncio.run(_create())
    try:
        token_response = client.post(
            "/api/admin/token",
            data={"username": username, "password": password, "grant_type": "password"},
        )
        assert token_response.status_code == status.HTTP_200_OK
        yield auth_headers(token_response.json()["access_token"])
    finally:
        asyncio.run(_cleanup(role_id, admin_id))


@pytest.mark.parametrize("endpoint", STATS_ENDPOINTS)
def test_owner_unknown_admin_filter_is_rejected(access_token, fake_resources, endpoint):
    response = client.get(
        endpoint,
        headers=auth_headers(access_token),
        params={"admin_username": unique_name("no_such_admin")},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == "Admin not found"


@pytest.mark.parametrize("endpoint", STATS_ENDPOINTS)
def test_owner_known_admin_filter_returns_that_admins_stats(access_token, fake_resources, endpoint):
    target = create_admin(access_token)
    try:
        response = client.get(
            endpoint,
            headers=auth_headers(access_token),
            params={"admin_username": target["username"]},
        )
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["total_user"] == 0
        assert body["admin_used_traffic"] == 0
        assert body["incoming_bandwidth"] == 0
        assert body["outgoing_bandwidth"] == 0
    finally:
        delete_admin(access_token, target["username"])


def test_admin_reader_unknown_admin_filter_cannot_fall_back_to_system_stats(access_token):
    with role_admin({"users": {"read": True}, "admins": {"read": True}}) as headers:
        response = client.get(
            "/api/system/users",
            headers=headers,
            params={"admin_username": unique_name("no_such_admin")},
        )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == "Admin not found"


def test_admin_reader_known_admin_filter_still_works(access_token):
    target = create_admin(access_token)
    try:
        with role_admin({"users": {"read": True}, "admins": {"read": True}}) as headers:
            response = client.get(
                "/api/system/users",
                headers=headers,
                params={"admin_username": target["username"]},
            )
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["admin_used_traffic"] == 0
    finally:
        delete_admin(access_token, target["username"])


def test_admin_without_admins_read_stays_scoped_to_itself(access_token):
    with role_admin({"users": {"read": True}}) as headers:
        response = client.get(
            "/api/system/users",
            headers=headers,
            params={"admin_username": unique_name("no_such_admin")},
        )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["admin_used_traffic"] == 0
    assert body["incoming_bandwidth"] == 0
    assert body["outgoing_bandwidth"] == 0
