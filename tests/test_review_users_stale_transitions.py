from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from app.db.models import NextPlan, User, UserStatus
from app.fork.jobs import status_review, usage_limits
from app.jobs import record_usages, review_users
from app.models.settings import General
from tests.test_recorder_harness import recorder_db_fixture, seed_users_and_nodes  # noqa: F401


@pytest.fixture(name="review_events")
def review_events_fixture(monkeypatch: pytest.MonkeyPatch, recorder_db) -> dict[str, list]:
    events: dict[str, list] = {"pushed": [], "status": [], "next_plan": []}

    async def record_push(users):
        events["pushed"].append([(user.id, user.status) for user in users])

    async def record_status_change(db_user, status):
        events["status"].append((db_user.id, status))

    async def record_next_plan(db_user):
        events["next_plan"].append(db_user.id)

    monkeypatch.setattr(review_users, "sync_users", record_push)
    monkeypatch.setattr(review_users, "_notify_status_change", record_status_change)
    monkeypatch.setattr(review_users, "_notify_next_plan", record_next_plan)
    monkeypatch.setattr(review_users, "GetDB", record_usages.GetDB)
    monkeypatch.setattr(status_review, "GetDB", record_usages.GetDB)
    monkeypatch.setattr(review_users, "general_settings", AsyncMock(return_value=General()))
    return events


async def _row(session_factory, user_id: int):
    async with session_factory() as session:
        return (
            await session.execute(
                select(User.status, User.expire, User.data_limit, User.used_traffic).where(User.id == user_id)
            )
        ).one()


@pytest.mark.asyncio
async def test_expire_job_does_not_overwrite_a_renewal_committed_after_its_read(
    monkeypatch: pytest.MonkeyPatch, recorder_db, review_events
):
    _, user_ids, _ = await seed_users_and_nodes(recorder_db, user_count=1, node_count=0)
    user_id = user_ids[0]
    past = datetime.now(UTC) - timedelta(minutes=5)
    renewed_until = datetime.now(UTC) + timedelta(days=30)
    async with recorder_db() as session:
        await session.execute(update(User).where(User.id == user_id).values(status=UserStatus.active, expire=past))
        await session.commit()

    real_candidates = review_users.get_active_to_expire_users

    async def candidates_then_admin_renews(db):
        stale = await real_candidates(db)
        async with recorder_db() as admin_session:
            await admin_session.execute(
                update(User).where(User.id == user_id).values(expire=renewed_until, status=UserStatus.active)
            )
            await admin_session.commit()
        return stale

    monkeypatch.setattr(review_users, "get_active_to_expire_users", candidates_then_admin_renews)

    await review_users.expire_users_job()

    row = await _row(recorder_db, user_id)
    assert row.expire.replace(tzinfo=UTC) > datetime.now(UTC)
    assert row.status == UserStatus.active, (row.status, row.expire, review_events)


@pytest.mark.asyncio
async def test_on_hold_job_does_not_reenable_a_user_disabled_after_its_read(
    monkeypatch: pytest.MonkeyPatch, recorder_db, review_events
):
    _, user_ids, _ = await seed_users_and_nodes(recorder_db, user_count=1, node_count=0)
    user_id = user_ids[0]
    async with recorder_db() as session:
        await session.execute(
            update(User)
            .where(User.id == user_id)
            .values(
                status=UserStatus.on_hold,
                expire=None,
                on_hold_expire_duration=86400,
                online_at=datetime.now(UTC) + timedelta(seconds=5),
            )
        )
        await session.commit()

    real_candidates = review_users.get_on_hold_to_active_users

    async def candidates_then_admin_disables(db):
        stale = await real_candidates(db)
        async with recorder_db() as admin_session:
            await admin_session.execute(
                update(User)
                .where(User.id == user_id)
                .values(status=UserStatus.disabled, edit_at=datetime.now(UTC) + timedelta(seconds=10))
            )
            await admin_session.commit()
        return stale

    monkeypatch.setattr(review_users, "get_on_hold_to_active_users", candidates_then_admin_disables)

    await review_users.on_hold_to_active_users_job()

    row = await _row(recorder_db, user_id)
    assert row.status == UserStatus.disabled, (row.status, review_events)


@pytest.mark.asyncio
async def test_expire_job_next_plan_does_not_credit_bytes_billed_after_its_read(
    monkeypatch: pytest.MonkeyPatch, recorder_db, review_events
):
    _, user_ids, _ = await seed_users_and_nodes(recorder_db, user_count=1, node_count=0)
    user_id = user_ids[0]
    past = datetime.now(UTC) - timedelta(minutes=5)
    async with recorder_db() as session:
        await session.execute(
            update(User)
            .where(User.id == user_id)
            .values(status=UserStatus.active, expire=past, data_limit=1000, used_traffic=400)
        )
        session.add(
            NextPlan(user_id=user_id, user_template_id=None, data_limit=5000, expire=None, add_remaining_traffic=True)
        )
        await session.commit()

    real_candidates = review_users.get_active_to_expire_users

    async def candidates_then_recorder_bills(db):
        stale = await real_candidates(db)
        async with recorder_db() as recorder_session:
            await recorder_session.execute(
                update(User).where(User.id == user_id).values(used_traffic=User.used_traffic + 300)
            )
            await recorder_session.commit()
        return stale

    monkeypatch.setattr(review_users, "get_active_to_expire_users", candidates_then_recorder_bills)

    await review_users.expire_users_job()

    row = await _row(recorder_db, user_id)
    assert review_events["next_plan"] == [user_id]
    assert row.data_limit == 5000 + (1000 - 700), row.data_limit


@pytest.mark.asyncio
async def test_limit_path_is_the_control_and_holds(monkeypatch: pytest.MonkeyPatch, recorder_db, review_events):
    _, user_ids, _ = await seed_users_and_nodes(recorder_db, user_count=1, node_count=0)
    user_id = user_ids[0]
    async with recorder_db() as session:
        await session.execute(update(User).where(User.id == user_id).values(data_limit=1000, used_traffic=1100))
        await session.commit()

    real_candidates = review_users.get_active_to_limited_users

    async def candidates_then_admin_raises_limit(db):
        stale = await real_candidates(db)
        async with recorder_db() as admin_session:
            await admin_session.execute(update(User).where(User.id == user_id).values(data_limit=50_000))
            await admin_session.commit()
        return stale

    monkeypatch.setattr(review_users, "get_active_to_limited_users", candidates_then_admin_raises_limit)
    monkeypatch.setattr(usage_limits, "GetDB", record_usages.GetDB)

    await review_users.limit_users_job()

    row = await _row(recorder_db, user_id)
    assert row.status == UserStatus.active


@pytest.mark.asyncio
async def test_expire_job_does_not_apply_a_next_plan_the_limit_path_already_applied(
    monkeypatch: pytest.MonkeyPatch, recorder_db, review_events
):
    from sqlalchemy import func

    from app.db.models import UserUsageResetLogs

    _, user_ids, _ = await seed_users_and_nodes(recorder_db, user_count=1, node_count=0)
    user_id = user_ids[0]
    past = datetime.now(UTC) - timedelta(minutes=5)
    async with recorder_db() as session:
        await session.execute(
            update(User)
            .where(User.id == user_id)
            .values(status=UserStatus.active, expire=past, data_limit=1000, used_traffic=1100)
        )
        session.add(
            NextPlan(user_id=user_id, user_template_id=None, data_limit=5000, expire=86400, add_remaining_traffic=False)
        )
        await session.commit()

    real_candidates = review_users.get_active_to_expire_users
    monkeypatch.setattr(usage_limits, "GetDB", record_usages.GetDB)

    async def candidates_then_limit_path_applies_plan_and_tick_bills(db):
        stale = await real_candidates(db)
        await usage_limits.enforce_user_limits_now([user_id])
        async with recorder_db() as recorder_session:
            await recorder_session.execute(
                update(User).where(User.id == user_id).values(used_traffic=User.used_traffic + 77)
            )
            await recorder_session.commit()
        return stale

    monkeypatch.setattr(
        review_users, "get_active_to_expire_users", candidates_then_limit_path_applies_plan_and_tick_bills
    )

    await review_users.expire_users_job()

    async with recorder_db() as session:
        row = (
            await session.execute(select(User.status, User.used_traffic, User.usage_epoch).where(User.id == user_id))
        ).one()
        reset_logs = (
            await session.execute(
                select(func.count()).select_from(UserUsageResetLogs).where(UserUsageResetLogs.user_id == user_id)
            )
        ).scalar_one()
    assert (reset_logs, int(row.usage_epoch), int(row.used_traffic)) == (1, 1, 77), (
        reset_logs,
        row,
        review_events,
    )


async def _seed_with_unlimited_next_plan(recorder_db, *, add_remaining_traffic: bool = False, **values) -> int:
    _, user_ids, _ = await seed_users_and_nodes(recorder_db, user_count=1, node_count=0)
    user_id = user_ids[0]
    async with recorder_db() as session:
        await session.execute(update(User).where(User.id == user_id).values(**values))
        session.add(
            NextPlan(
                user_id=user_id,
                user_template_id=None,
                data_limit=0,
                expire=0,
                add_remaining_traffic=add_remaining_traffic,
            )
        )
        await session.commit()
    return user_id


async def _next_plan_ids(recorder_db, user_id: int) -> list[int]:
    async with recorder_db() as session:
        return list((await session.execute(select(NextPlan.id).where(NextPlan.user_id == user_id))).scalars().all())


@pytest.mark.asyncio
async def test_expire_job_applies_a_deliberately_unlimited_next_plan(recorder_db, review_events):
    user_id = await _seed_with_unlimited_next_plan(
        recorder_db,
        status=UserStatus.active,
        expire=datetime.now(UTC) - timedelta(minutes=5),
        data_limit=1000,
        used_traffic=10,
    )

    await review_users.expire_users_job()

    row = await _row(recorder_db, user_id)
    assert (row.status, row.data_limit, row.expire) == (UserStatus.active, 0, None), (row, review_events)
    assert review_events["next_plan"] == [user_id]
    assert await _next_plan_ids(recorder_db, user_id) == []


@pytest.mark.asyncio
async def test_limit_path_applies_a_deliberately_unlimited_next_plan(
    monkeypatch: pytest.MonkeyPatch, recorder_db, review_events
):
    monkeypatch.setattr(usage_limits, "GetDB", record_usages.GetDB)
    user_id = await _seed_with_unlimited_next_plan(
        recorder_db, status=UserStatus.active, data_limit=1000, used_traffic=1100
    )

    await usage_limits.enforce_user_limits_now([user_id])

    row = await _row(recorder_db, user_id)
    assert (row.status, row.data_limit, row.used_traffic, row.expire) == (UserStatus.active, 0, 0, None), (
        row,
        review_events,
    )
    assert review_events["next_plan"] == [user_id]


@pytest.mark.asyncio
async def test_expire_job_applies_an_open_ended_next_plan_that_keeps_the_remaining_traffic(recorder_db, review_events):
    user_id = await _seed_with_unlimited_next_plan(
        recorder_db,
        add_remaining_traffic=True,
        status=UserStatus.active,
        expire=datetime.now(UTC) - timedelta(minutes=5),
        data_limit=1000,
        used_traffic=400,
    )

    await review_users.expire_users_job()

    row = await _row(recorder_db, user_id)
    assert (row.status, row.data_limit, row.used_traffic, row.expire) == (UserStatus.active, 600, 0, None), (
        row,
        review_events,
    )
    assert review_events["next_plan"] == [user_id]


@pytest.mark.asyncio
async def test_expire_job_still_applies_a_real_next_plan(recorder_db, review_events):
    _, user_ids, _ = await seed_users_and_nodes(recorder_db, user_count=1, node_count=0)
    user_id = user_ids[0]
    async with recorder_db() as session:
        await session.execute(
            update(User)
            .where(User.id == user_id)
            .values(status=UserStatus.active, expire=datetime.now(UTC) - timedelta(minutes=5), data_limit=1000)
        )
        session.add(NextPlan(user_id=user_id, user_template_id=None, data_limit=5000, expire=86400))
        await session.commit()

    await review_users.expire_users_job()

    row = await _row(recorder_db, user_id)
    assert (row.status, row.data_limit) == (UserStatus.active, 5000)
    assert row.expire.replace(tzinfo=UTC) > datetime.now(UTC)
    assert review_events["next_plan"] == [user_id]
