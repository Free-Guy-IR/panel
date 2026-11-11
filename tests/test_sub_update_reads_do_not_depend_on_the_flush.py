from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.db.crud.user import (
    get_users_sub_update_list,
    get_users_subscription_agent_counts,
    get_users_subscription_agent_stats,
)
from app.db.models import UserSubscriptionUpdate
from app.jobs import record_usages
from app.models.stats import Period
from app.subscription import sub_update_buffer
from tests.test_recorder_harness import recorder_db_fixture, seed_users_and_nodes  # noqa: F401

READERS = ("list", "counts", "stats")
BOUND = 5


def _deadlock() -> OperationalError:
    return OperationalError("INSERT INTO user_subscription_updates", {}, Exception(1213, "Deadlock found"))


class _FailingGetDB:
    async def __aenter__(self):
        raise _deadlock()

    async def __aexit__(self, exc_type, exc_value, traceback):
        return False


def _stuck_get_db(release: asyncio.Event, entered: asyncio.Event):
    real_get_db = record_usages.GetDB

    class _StuckGetDB:
        def __init__(self):
            self.inner = real_get_db()

        async def __aenter__(self):
            entered.set()
            await release.wait()
            return await self.inner.__aenter__()

        async def __aexit__(self, exc_type, exc_value, traceback):
            return await self.inner.__aexit__(exc_type, exc_value, traceback)

    return _StuckGetDB


async def _read(recorder_db, reader: str, user_id: int) -> list[tuple[str, int]]:
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    end = start + timedelta(hours=3)
    async with recorder_db() as request:
        if reader == "list":
            rows, count = await get_users_sub_update_list(request, user_id)
            assert count == len(rows)
            return sorted((row.user_agent, 1) for row in rows)
        if reader == "counts":
            return sorted(await get_users_subscription_agent_counts(request, user_id=user_id, start=start, end=end))
        stats = await get_users_subscription_agent_stats(request, start, end, Period.hour, user_id=user_id)
        return sorted((row["agent"], row["count"]) for row in stats)


async def _stored_agents(recorder_db, user_id: int) -> list[str]:
    async with recorder_db() as session:
        return sorted(
            (
                await session.execute(
                    select(UserSubscriptionUpdate.user_agent).where(UserSubscriptionUpdate.user_id == user_id)
                )
            ).scalars()
        )


@pytest.fixture(name="subscribed_user")
async def subscribed_user_fixture(recorder_db, monkeypatch: pytest.MonkeyPatch):
    await sub_update_buffer.reset_user_sub_update_buffer()
    monkeypatch.setattr(sub_update_buffer, "GetDB", record_usages.GetDB)
    _, (user_id,), _ = await seed_users_and_nodes(recorder_db, user_count=1, node_count=0)
    async with recorder_db() as session:
        session.add(UserSubscriptionUpdate(user_id=user_id, user_agent="committed-client"))
        await session.commit()
    await sub_update_buffer.queue_user_sub_update(user_id, "pending-client")
    yield user_id
    await asyncio.gather(*list(sub_update_buffer._background_flushes), return_exceptions=True)
    await sub_update_buffer.reset_user_sub_update_buffer()


@pytest.mark.asyncio
@pytest.mark.parametrize("reader", READERS)
async def test_a_subscription_update_read_answers_when_the_flush_deadlocks(
    recorder_db, subscribed_user, monkeypatch: pytest.MonkeyPatch, reader
):
    monkeypatch.setattr(sub_update_buffer, "GetDB", _FailingGetDB)

    answer = await asyncio.wait_for(_read(recorder_db, reader, subscribed_user), BOUND)

    assert answer == [("committed-client", 1)]
    assert sub_update_buffer.pending_count() == 1
    monkeypatch.setattr(sub_update_buffer, "GetDB", record_usages.GetDB)
    assert await sub_update_buffer.flush_user_sub_updates() == 1
    assert await _read(recorder_db, reader, subscribed_user) == [("committed-client", 1), ("pending-client", 1)]


@pytest.mark.asyncio
@pytest.mark.parametrize("reader", READERS)
async def test_a_subscription_update_read_does_not_wait_for_a_stuck_flush_and_the_row_still_lands_once(
    recorder_db, subscribed_user, monkeypatch: pytest.MonkeyPatch, reader
):
    release, entered = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(sub_update_buffer, "GetDB", _stuck_get_db(release, entered))
    stuck_flush = asyncio.create_task(sub_update_buffer.flush_user_sub_updates())
    await asyncio.wait_for(entered.wait(), BOUND)

    started = time.monotonic()
    try:
        answer = await asyncio.wait_for(_read(recorder_db, reader, subscribed_user), BOUND)
    finally:
        release.set()
    elapsed = time.monotonic() - started

    assert answer == [("committed-client", 1)]
    assert elapsed < BOUND
    assert await asyncio.wait_for(stuck_flush, BOUND) == 1
    await asyncio.gather(*list(sub_update_buffer._background_flushes), return_exceptions=True)
    assert sub_update_buffer.pending_count() == 0
    assert await _stored_agents(recorder_db, subscribed_user) == ["committed-client", "pending-client"]


@pytest.mark.asyncio
async def test_a_cancelled_subscription_update_read_does_not_cancel_the_flush_it_started(
    recorder_db, subscribed_user, monkeypatch: pytest.MonkeyPatch
):
    release, entered = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(sub_update_buffer, "GetDB", _stuck_get_db(release, entered))
    reader = asyncio.create_task(_read(recorder_db, "list", subscribed_user))
    await asyncio.wait_for(entered.wait(), BOUND)

    reader.cancel()
    await asyncio.gather(reader, return_exceptions=True)
    release.set()
    await asyncio.wait_for(asyncio.gather(*list(sub_update_buffer._background_flushes), return_exceptions=True), BOUND)
    if sub_update_buffer.pending_count():
        assert await sub_update_buffer.flush_user_sub_updates() == 1

    assert sub_update_buffer.pending_count() == 0
    assert await _stored_agents(recorder_db, subscribed_user) == ["committed-client", "pending-client"]
