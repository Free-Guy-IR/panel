from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select, update

from app.db.models import NextPlan, User, UserStatus, UserUsageResetLogs
from app.fork.jobs import usage_limits
from app.jobs import record_usages, review_users
from tests.test_recorder_harness import recorder_db_fixture, seed_users_and_nodes  # noqa: F401


@pytest.fixture(name="limit_events")
def limit_events_fixture(monkeypatch: pytest.MonkeyPatch, recorder_db) -> dict[str, list]:
    events: dict[str, list] = {"pushed": [], "status": [], "next_plan": []}

    async def record_push(users):
        events["pushed"].append(sorted(user.id for user in users))

    async def record_status_change(db_user, status):
        events["status"].append((db_user.id, status))

    async def record_next_plan(db_user):
        events["next_plan"].append(db_user.id)

    monkeypatch.setattr(review_users, "sync_users", record_push)
    monkeypatch.setattr(review_users, "_notify_status_change", record_status_change)
    monkeypatch.setattr(review_users, "_notify_next_plan", record_next_plan)
    monkeypatch.setattr(review_users, "GetDB", record_usages.GetDB)
    return events


async def _user_over_its_limit(session_factory, with_next_plan: bool) -> int:
    _, user_ids, _ = await seed_users_and_nodes(session_factory, user_count=1, node_count=0)
    user_id = user_ids[0]
    async with session_factory() as session:
        await session.execute(update(User).where(User.id == user_id).values(data_limit=1000, used_traffic=1100))
        if with_next_plan:
            session.add(
                NextPlan(
                    user_id=user_id, user_template_id=None, data_limit=5000, expire=None, add_remaining_traffic=False
                )
            )
        await session.commit()
    return user_id


async def _bill(session_factory, user_id: int, value: int) -> None:
    async with session_factory() as session:
        await session.execute(update(User).where(User.id == user_id).values(used_traffic=User.used_traffic + value))
        await session.commit()


async def _final_state(session_factory, user_id: int) -> tuple[UserStatus, int, int, int]:
    async with session_factory() as session:
        row = (
            await session.execute(
                select(User.status, User.used_traffic, User.data_limit, User.usage_epoch).where(User.id == user_id)
            )
        ).one()
        reset_logs = (
            await session.execute(
                select(func.count()).select_from(UserUsageResetLogs).where(UserUsageResetLogs.user_id == user_id)
            )
        ).scalar_one()
    return row.status, int(row.used_traffic), int(row.usage_epoch), int(reset_logs)


def _tick_lands_after_the_review_read(monkeypatch: pytest.MonkeyPatch, session_factory, billed_afterwards: int):
    real_candidates = review_users.get_active_to_limited_users

    async def candidates_then_tick(db):
        stale = await real_candidates(db)
        await usage_limits.enforce_user_limits_now([user.id for user in stale])
        for user in stale:
            await _bill(session_factory, user.id, billed_afterwards)
        return stale

    monkeypatch.setattr(review_users, "get_active_to_limited_users", candidates_then_tick)


@pytest.mark.asyncio
async def test_a_plain_user_is_limited_once_when_the_tick_beats_a_stale_review(
    monkeypatch: pytest.MonkeyPatch, recorder_db, limit_events
):
    user_id = await _user_over_its_limit(recorder_db, with_next_plan=False)
    _tick_lands_after_the_review_read(monkeypatch, recorder_db, billed_afterwards=40)

    await review_users.limit_users_job()

    status, used_traffic, usage_epoch, reset_logs = await _final_state(recorder_db, user_id)
    assert (status, used_traffic, usage_epoch, reset_logs) == (UserStatus.limited, 1140, 0, 0)
    assert limit_events["pushed"] == [[user_id]]
    assert limit_events["status"] == [(user_id, UserStatus.limited)]


@pytest.mark.asyncio
async def test_a_next_plan_is_applied_once_when_the_tick_beats_a_stale_review(
    monkeypatch: pytest.MonkeyPatch, recorder_db, limit_events
):
    user_id = await _user_over_its_limit(recorder_db, with_next_plan=True)
    _tick_lands_after_the_review_read(monkeypatch, recorder_db, billed_afterwards=150)

    await review_users.limit_users_job()

    status, used_traffic, usage_epoch, reset_logs = await _final_state(recorder_db, user_id)
    assert (status, used_traffic, usage_epoch, reset_logs) == (UserStatus.active, 150, 1, 1)
    assert limit_events["next_plan"] == [user_id]
    assert limit_events["pushed"] == [[user_id]]
    assert limit_events["status"] == []


@pytest.mark.asyncio
async def test_a_second_transition_waits_for_the_row_lock_and_then_skips(recorder_db, limit_events):
    if record_usages.engine.dialect.name == "sqlite":
        pytest.skip("row locks need a server database")
    user_id = await _user_over_its_limit(recorder_db, with_next_plan=False)

    async with recorder_db() as holder:
        await holder.execute(select(User.id).where(User.id == user_id).with_for_update())
        second = asyncio.ensure_future(usage_limits.enforce_user_limits_now([user_id]))
        await asyncio.sleep(1.0)
        blocked_while_locked = not second.done()
        await holder.execute(update(User).where(User.id == user_id).values(status=UserStatus.limited))
        await holder.commit()

    await asyncio.wait_for(second, timeout=15.0)

    assert blocked_while_locked
    assert limit_events == {"pushed": [], "status": [], "next_plan": []}
    status, _, _, _ = await _final_state(recorder_db, user_id)
    assert status == UserStatus.limited
