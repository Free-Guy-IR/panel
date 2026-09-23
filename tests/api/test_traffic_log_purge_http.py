import asyncio
import importlib
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import status
from sqlalchemy import delete, func, select

from app.fork.models.traffic_log import TrafficLogRecord
from app.fork.routers.traffic_log import OWNER_MESSAGE
from app.fork.traffic_log.collector import TrafficCollector
from tests.api import GetTestDB, TestSession, client
from tests.api.helpers import auth_headers, create_admin, delete_admin, unique_name

PURGE_URL = "/api/traffic-log/purge"
PURGE_NODE_ID = 9451


async def _clear_rows() -> None:
    async with TestSession() as session:
        await session.execute(delete(TrafficLogRecord))
        await session.commit()


async def _insert_rows(*ages: timedelta) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    async with TestSession() as session:
        for index, age in enumerate(ages):
            seen = now - age
            session.add(
                TrafficLogRecord(
                    bucket_start=seen,
                    node_id=PURGE_NODE_ID,
                    inbound_tag="purge-http",
                    host=f"host-{index}.example",
                    port=443,
                    protocol="tcp",
                    route="DIRECT",
                    first_seen=seen,
                    last_seen=seen,
                )
            )
        await session.commit()


async def _row_count() -> int:
    async with TestSession() as session:
        return int(await session.scalar(select(func.count()).select_from(TrafficLogRecord)) or 0)


@pytest.fixture(autouse=True)
def isolated_purge(monkeypatch: pytest.MonkeyPatch):
    traffic_log_package = importlib.import_module("app.fork.traffic_log")
    collector_module = importlib.import_module("app.fork.traffic_log.collector")
    monkeypatch.setattr("app.fork.routers.traffic_log.GetDB", GetTestDB)
    monkeypatch.setattr(collector_module, "GetDB", GetTestDB)
    monkeypatch.setattr(traffic_log_package, "collector", TrafficCollector())
    asyncio.run(_clear_rows())
    yield
    asyncio.run(_clear_rows())


@pytest.fixture
def logs_role(access_token):
    response = client.post(
        "/api/admin-role",
        headers=auth_headers(access_token),
        json={
            "name": unique_name("traffic_logs"),
            "permissions": {"nodes": {"read": True, "logs": True}},
            "limits": {
                "max_users": None,
                "data_limit_min": None,
                "data_limit_max": None,
                "expire_min": None,
                "expire_max": None,
                "max_hwid_per_user": None,
            },
            "features": {"can_use_reset_strategy": True, "can_use_next_plan": True},
            "access": {"require_template": False, "allowed_template_ids": None, "allowed_group_ids": None},
        },
    )
    assert response.status_code == status.HTTP_201_CREATED, response.text
    role = response.json()
    yield role
    client.delete(f"/api/admin-role/{role['id']}", headers=auth_headers(access_token))


def test_the_owner_purges_only_rows_older_than_the_cutoff(access_token):
    asyncio.run(_insert_rows(timedelta(hours=5), timedelta(hours=3), timedelta(hours=2), timedelta(minutes=5)))

    response = client.post(PURGE_URL, headers=auth_headers(access_token), json={"older_than_hours": 1})

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert body["removed"] == 3
    assert body["incomplete"] is False
    assert body["remaining"] == 1
    assert asyncio.run(_row_count()) == 1


def test_a_purge_without_a_body_removes_every_row(access_token):
    asyncio.run(_insert_rows(timedelta(hours=30), timedelta(hours=1), timedelta(seconds=10)))

    response = client.post(PURGE_URL, headers=auth_headers(access_token))

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert body["removed"] == 3
    assert body["remaining"] == 0
    assert asyncio.run(_row_count()) == 0


def test_a_non_owner_with_the_logs_permission_is_refused_and_nothing_is_removed(access_token, logs_role):
    asyncio.run(_insert_rows(timedelta(hours=5), timedelta(minutes=1)))
    admin = create_admin(access_token, role_id=logs_role["id"])
    try:
        login = client.post(
            "/api/admin/token",
            data={"username": admin["username"], "password": admin["password"], "grant_type": "password"},
        )
        assert login.status_code == status.HTTP_200_OK, login.text
        token = login.json()["access_token"]

        refused = client.post(PURGE_URL, headers=auth_headers(token), json={"older_than_hours": 1})
        assert refused.status_code == status.HTTP_403_FORBIDDEN
        assert refused.json()["detail"] == OWNER_MESSAGE

        refused_everything = client.post(PURGE_URL, headers=auth_headers(token))
        assert refused_everything.status_code == status.HTTP_403_FORBIDDEN

        assert asyncio.run(_row_count()) == 2
    finally:
        delete_admin(access_token, admin["username"])
