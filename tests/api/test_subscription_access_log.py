import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import status
from sqlalchemy import delete, select

from app.db.models import SubscriptionAccessKind, User, UserHWID, UserSubscriptionAccess, UserSubscriptionUpdate
from app.jobs import (
    cleanup_subscription_accesses as accesses_job,
    cleanup_subscription_updates as updates_job,
)
from app.subscription import access_buffer, sub_update_buffer
from config import subscription_env_settings
from tests.api import GetTestDB, TestSession, client
from tests.api.helpers import (
    auth_headers,
    create_core,
    create_group,
    create_user,
    delete_core,
    delete_group,
    delete_user,
    unique_name,
)

BROWSER_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0",
}
CLIENT_USER_AGENT = "v2rayNG/1.9.46"


@pytest.fixture(autouse=True)
def local_db(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(access_buffer, "GetDB", GetTestDB)
    monkeypatch.setattr(sub_update_buffer, "GetDB", GetTestDB)
    monkeypatch.setattr(accesses_job, "GetDB", GetTestDB)
    monkeypatch.setattr(updates_job, "GetDB", GetTestDB)
    asyncio.run(access_buffer.reset_subscription_access_buffer())
    yield
    asyncio.run(access_buffer.reset_subscription_access_buffer())


@pytest.fixture
def sub_user(access_token):
    core = create_core(access_token)
    group = create_group(access_token, name=unique_name("access_log_group"))
    user = create_user(
        access_token,
        group_ids=[group["id"]],
        payload={"username": unique_name("access_log_user")},
    )
    user["id"] = user_id_of(user["username"])
    wipe_rows(user["id"])
    try:
        yield user
    finally:
        asyncio.run(access_buffer.reset_subscription_access_buffer())
        asyncio.run(sub_update_buffer.flush_user_sub_updates())
        wipe_rows(user["id"])
        delete_user(access_token, user["username"])
        delete_group(access_token, group["id"])
        delete_core(access_token, core["id"])


def user_id_of(username: str) -> int:
    async def _fetch() -> int:
        async with TestSession() as session:
            return (await session.execute(select(User.id).where(User.username == username))).scalar_one()

    return asyncio.run(_fetch())


def accesses_of(user_id: int) -> list[UserSubscriptionAccess]:
    async def _fetch():
        await access_buffer.flush_subscription_accesses()
        async with TestSession() as session:
            result = await session.execute(
                select(UserSubscriptionAccess)
                .where(UserSubscriptionAccess.user_id == user_id)
                .order_by(UserSubscriptionAccess.id)
            )
            return list(result.scalars().all())

    return asyncio.run(_fetch())


def client_updates_of(user_id: int) -> list[UserSubscriptionUpdate]:
    async def _fetch():
        await sub_update_buffer.flush_user_sub_updates()
        async with TestSession() as session:
            result = await session.execute(
                select(UserSubscriptionUpdate)
                .where(UserSubscriptionUpdate.user_id == user_id)
                .order_by(UserSubscriptionUpdate.id)
            )
            return list(result.scalars().all())

    return asyncio.run(_fetch())


def hwids_of(user_id: int) -> list[str]:
    async def _fetch():
        async with TestSession() as session:
            result = await session.execute(select(UserHWID.hwid).where(UserHWID.user_id == user_id))
            return list(result.scalars().all())

    return asyncio.run(_fetch())


def seed_rows(user_id: int, kind: str, count: int, base: datetime) -> None:
    async def _seed():
        async with TestSession() as session:
            for index in range(count):
                row = UserSubscriptionAccess(user_id=user_id, access_kind=kind, user_agent=f"seed-{index}", ip=None)
                row.created_at = base + timedelta(seconds=index)
                session.add(row)
            await session.commit()

    asyncio.run(_seed())


def seed_client_updates(user_id: int, count: int, base: datetime) -> None:
    async def _seed():
        async with TestSession() as session:
            for index in range(count):
                row = UserSubscriptionUpdate(user_id=user_id, user_agent=f"client-{index}", ip=None, hwid=None)
                row.created_at = base + timedelta(seconds=index)
                session.add(row)
            await session.commit()

    asyncio.run(_seed())


def wipe_rows(user_id: int) -> None:
    async def _wipe():
        async with TestSession() as session:
            await session.execute(delete(UserSubscriptionAccess).where(UserSubscriptionAccess.user_id == user_id))
            await session.execute(delete(UserSubscriptionUpdate).where(UserSubscriptionUpdate.user_id == user_id))
            await session.commit()

    asyncio.run(_wipe())


def test_browser_page_view_is_recorded(sub_user, access_token):
    response = client.get(sub_user["subscription_url"], headers=BROWSER_HEADERS)

    assert response.status_code == status.HTTP_200_OK
    assert "text/html" in response.headers["content-type"]

    accesses = accesses_of(sub_user["id"])
    assert len(accesses) == 1
    assert accesses[0].access_kind in (
        SubscriptionAccessKind.page_view.value,
        SubscriptionAccessKind.page_config.value,
    )
    assert accesses[0].user_agent == BROWSER_HEADERS["User-Agent"]

    sub_updates = client.get(
        f"/api/user/{sub_user['username']}/sub_update",
        headers=auth_headers(access_token),
    )
    assert sub_updates.status_code == status.HTTP_200_OK
    assert sub_updates.json()["count"] == 0


def test_info_endpoint_is_recorded(sub_user):
    response = client.get(f"{sub_user['subscription_url']}/info", headers={"User-Agent": CLIENT_USER_AGENT})

    assert response.status_code == status.HTTP_200_OK

    accesses = accesses_of(sub_user["id"])
    assert [access.access_kind for access in accesses] == [SubscriptionAccessKind.info.value]
    assert accesses[0].user_agent == CLIENT_USER_AGENT
    assert client_updates_of(sub_user["id"]) == []


def test_head_request_is_recorded_as_its_own_kind(sub_user):
    response = client.head(sub_user["subscription_url"], headers={"User-Agent": CLIENT_USER_AGENT})

    assert response.status_code == status.HTTP_200_OK

    accesses = accesses_of(sub_user["id"])
    assert [access.access_kind for access in accesses] == [SubscriptionAccessKind.head.value]
    assert client_updates_of(sub_user["id"]) == []


def test_manual_client_type_is_recorded(sub_user):
    response = client.get(f"{sub_user['subscription_url']}/links", headers={"User-Agent": "Reseller-Bot/1.0"})

    assert response.status_code == status.HTTP_200_OK

    accesses = accesses_of(sub_user["id"])
    assert [access.access_kind for access in accesses] == [SubscriptionAccessKind.manual.value]
    assert accesses[0].user_agent == "Reseller-Bot/1.0"
    assert client_updates_of(sub_user["id"]) == []


def test_raw_endpoint_is_recorded(sub_user):
    response = client.get(f"{sub_user['subscription_url']}/raw", headers={"User-Agent": "PanelPreview/1.0"})

    assert response.status_code == status.HTTP_200_OK

    accesses = accesses_of(sub_user["id"])
    assert [access.access_kind for access in accesses] == [SubscriptionAccessKind.raw.value]


def test_apps_endpoint_is_recorded(sub_user):
    response = client.get(f"{sub_user['subscription_url']}/apps", headers={"User-Agent": CLIENT_USER_AGENT})

    assert response.status_code == status.HTTP_200_OK

    accesses = accesses_of(sub_user["id"])
    assert [access.access_kind for access in accesses] == [SubscriptionAccessKind.apps.value]
    assert client_updates_of(sub_user["id"]) == []


def test_usage_endpoint_is_recorded(sub_user):
    response = client.get(f"{sub_user['subscription_url']}/usage", headers={"User-Agent": CLIENT_USER_AGENT})

    assert response.status_code == status.HTTP_200_OK

    accesses = accesses_of(sub_user["id"])
    assert [access.access_kind for access in accesses] == [SubscriptionAccessKind.usage.value]
    assert client_updates_of(sub_user["id"]) == []


def test_client_fetch_behaviour_is_unchanged(sub_user, access_token):
    ip = "203.0.113.44"
    response = client.get(
        sub_user["subscription_url"],
        headers={"User-Agent": CLIENT_USER_AGENT, "X-Forwarded-For": ip, "X-HWID": "hwid-access-log"},
    )

    assert response.status_code == status.HTTP_200_OK

    sub_updates = client.get(
        f"/api/user/{sub_user['username']}/sub_update",
        headers=auth_headers(access_token),
    )
    assert sub_updates.status_code == status.HTTP_200_OK
    payload = sub_updates.json()
    assert payload["count"] == 1
    assert payload["updates"][0]["user_agent"] == CLIENT_USER_AGENT
    assert payload["updates"][0]["ip"] == ip

    assert hwids_of(sub_user["id"]) == ["hwid-access-log"]
    assert accesses_of(sub_user["id"]) == []


def test_repeated_page_views_are_coalesced(sub_user):
    for _ in range(5):
        assert client.get(sub_user["subscription_url"], headers=BROWSER_HEADERS).status_code == status.HTTP_200_OK

    assert len(accesses_of(sub_user["id"])) == 1


def test_pruning_keeps_client_rows_and_prunes_kinds_independently(sub_user):
    user_id = sub_user["id"]
    wipe_rows(user_id)
    base = datetime.now(UTC) - timedelta(hours=2)

    seed_client_updates(user_id, 3, base)
    seed_rows(user_id, SubscriptionAccessKind.page_view.value, 25, base)
    seed_rows(user_id, SubscriptionAccessKind.info.value, 12, base)

    asyncio.run(updates_job.cleanup_user_subscription_updates())
    asyncio.run(accesses_job.cleanup_user_subscription_accesses())

    surviving_updates = client_updates_of(user_id)
    assert len(surviving_updates) == 3
    assert [update.user_agent for update in surviving_updates] == ["client-0", "client-1", "client-2"]

    limit = subscription_env_settings.access_limit
    surviving = accesses_of(user_id)
    kinds = [access.access_kind for access in surviving]
    assert kinds.count(SubscriptionAccessKind.page_view.value) == limit
    assert kinds.count(SubscriptionAccessKind.info.value) == limit

    newest_page_views = {
        access.user_agent for access in surviving if access.access_kind == SubscriptionAccessKind.page_view.value
    }
    assert "seed-24" in newest_page_views
    assert "seed-0" not in newest_page_views
