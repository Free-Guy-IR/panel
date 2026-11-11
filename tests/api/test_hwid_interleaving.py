import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException, status
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.crud.hwid import get_user_hwids, register_user_hwid
from app.db.models import UserHWID
from app.models.settings import HWIDSettings
from tests.api import DATABASE_URL, IS_SQLITE, TestSession
from tests.api.helpers import create_user, delete_user
from tests.api.test_hwid import _reset_user_hwids, _set_user_hwid_limit


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _race_subscription_fetches(monkeypatch, user_id: int, hwid_limit: int, hwids: list[str]) -> list[bool]:
    from app.operation import OperatorType, subscription as subscription_module

    async def enabled_hwid_settings() -> HWIDSettings:
        return HWIDSettings(enabled=True, fallback_limit=3)

    every_racer_has_read = asyncio.Barrier(len(hwids))
    read_known_device = subscription_module.get_user_hwid_by_value

    async def read_then_wait_for_the_others(db, owner_id, value):
        known = await read_known_device(db, owner_id, value)
        async with asyncio.timeout(30):
            await every_racer_has_read.wait()
        return known

    monkeypatch.setattr(subscription_module, "hwid_settings", enabled_hwid_settings)
    monkeypatch.setattr(subscription_module, "get_user_hwid_by_value", read_then_wait_for_the_others)
    operation = subscription_module.SubscriptionOperation(OperatorType.API)
    racing_engine = create_async_engine(
        DATABASE_URL, poolclass=NullPool, connect_args={"timeout": 30} if IS_SQLITE else {}
    )
    racing_session = async_sessionmaker(racing_engine, expire_on_commit=False)

    async def fetch(hwid: str) -> bool:
        async with racing_session() as session:
            try:
                await operation.validate_and_register_hwid(
                    session, user_id, hwid_limit, None, hwid, "Android", "15", "Pixel 9"
                )
            except HTTPException as exc:
                assert exc.status_code == status.HTTP_403_FORBIDDEN
                assert exc.detail == "Device limit reached"
                return False
            return True

    try:
        return list(await asyncio.gather(*(fetch(hwid) for hwid in hwids)))
    finally:
        await racing_engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_subscription_fetches_from_new_devices_respect_the_limit(access_token, monkeypatch):
    if ":memory:" in DATABASE_URL:
        pytest.skip("needs a database that separate connections share")

    user = create_user(access_token)
    hwids = [f"fetch-race-device-{index}" for index in range(6)]

    try:
        await _set_user_hwid_limit(user["id"], 1)

        results = await _race_subscription_fetches(monkeypatch, user["id"], 1, hwids)

        assert sorted(results) == [False, False, False, False, False, True]
        async with TestSession() as session:
            stored = [row.hwid for row in await get_user_hwids(session, user["id"])]
        assert stored == [hwids[results.index(True)]]
    finally:
        await _reset_user_hwids(user["id"])
        delete_user(access_token, user["username"])


@pytest.mark.asyncio
async def test_known_device_refreshes_racing_new_devices_are_never_refused(access_token, monkeypatch):
    if ":memory:" in DATABASE_URL:
        pytest.skip("needs a database that separate connections share")

    user = create_user(access_token)
    known = "fetch-race-known-device"
    hwids = [known, "fetch-race-new-0", "fetch-race-new-1", known, "fetch-race-new-2", "fetch-race-new-3", known]
    stale = datetime.now(UTC) - timedelta(minutes=10)

    try:
        await _set_user_hwid_limit(user["id"], 2)
        async with TestSession() as session:
            await register_user_hwid(session, user["id"], known, "iOS", "17", "iPhone 15")
            await session.execute(
                update(UserHWID)
                .where(UserHWID.user_id == user["id"], UserHWID.hwid == known)
                .values(last_used_at=stale)
            )
            await session.commit()

        results = await _race_subscription_fetches(monkeypatch, user["id"], 2, hwids)

        outcomes = list(zip(hwids, results, strict=True))
        assert [result for hwid, result in outcomes if hwid == known] == [True, True, True]
        assert sorted(result for hwid, result in outcomes if hwid != known) == [False, False, False, True]
        winner = next(hwid for hwid, result in outcomes if hwid != known and result)
        async with TestSession() as session:
            rows = {row.hwid: row for row in await get_user_hwids(session, user["id"])}
        assert set(rows) == {known, winner}
        assert _as_utc(rows[known].last_used_at) > stale
        assert (rows[known].device_os, rows[known].os_version, rows[known].device_model) == ("iOS", "17", "iPhone 15")
    finally:
        await _reset_user_hwids(user["id"])
        delete_user(access_token, user["username"])
