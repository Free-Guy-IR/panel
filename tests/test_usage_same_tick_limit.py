from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from app.db.models import NextPlan, User, UserStatus
from app.jobs import record_usages, review_users
from tests.test_recorder_harness import (  # noqa: F401
    CountingNode,
    recorder_db_fixture,
    seed_users_and_nodes,
    usage_snapshot,
)


@pytest.fixture(name="limit_events")
def limit_events_fixture(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
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
    return events


def _install(monkeypatch: pytest.MonkeyPatch, nodes: list[CountingNode]) -> None:
    monkeypatch.setattr(
        record_usages.node_manager,
        "get_healthy_nodes",
        AsyncMock(return_value=[(node.node_id, node) for node in nodes]),
    )


async def _set_user(session_factory, user_id: int, **values) -> None:
    async with session_factory() as session:
        await session.execute(update(User).where(User.id == user_id).values(**values))
        await session.commit()


async def _user_state(session_factory, user_id: int) -> tuple[UserStatus, int, int | None]:
    async with session_factory() as session:
        row = (
            await session.execute(select(User.status, User.used_traffic, User.data_limit).where(User.id == user_id))
        ).one()
    return row.status, int(row.used_traffic), row.data_limit


async def _one_user_over_the_line(monkeypatch: pytest.MonkeyPatch, recorder_db, **values) -> int:
    _, user_ids, node_ids = await seed_users_and_nodes(recorder_db, user_count=1, node_count=1)
    user_id = user_ids[0]
    await _set_user(recorder_db, user_id, data_limit=1000, used_traffic=900, **values)
    _install(monkeypatch, [CountingNode(node_ids[0], {user_id: 200})])
    return user_id


@pytest.mark.asyncio
async def test_an_active_user_crossing_the_limit_is_limited_in_the_same_tick(
    monkeypatch: pytest.MonkeyPatch, recorder_db, limit_events
):
    user_id = await _one_user_over_the_line(monkeypatch, recorder_db)

    await record_usages._record_user_usages_impl()

    assert await _user_state(recorder_db, user_id) == (UserStatus.limited, 1100, 1000)
    assert limit_events["pushed"] == [[user_id]]
    assert limit_events["status"] == [(user_id, UserStatus.limited)]
    assert limit_events["next_plan"] == []


@pytest.mark.asyncio
async def test_an_on_hold_user_crossing_the_limit_is_limited_in_the_same_tick(
    monkeypatch: pytest.MonkeyPatch, recorder_db, limit_events
):
    user_id = await _one_user_over_the_line(monkeypatch, recorder_db, status=UserStatus.on_hold)

    await record_usages._record_user_usages_impl()

    assert await _user_state(recorder_db, user_id) == (UserStatus.limited, 1100, 1000)
    assert limit_events["pushed"] == [[user_id]]
    assert limit_events["status"] == [(user_id, UserStatus.limited)]


@pytest.mark.asyncio
async def test_a_user_with_a_next_plan_gets_the_plan_instead_of_the_limit(
    monkeypatch: pytest.MonkeyPatch, recorder_db, limit_events
):
    user_id = await _one_user_over_the_line(monkeypatch, recorder_db)
    async with recorder_db() as session:
        session.add(
            NextPlan(user_id=user_id, user_template_id=None, data_limit=5000, expire=None, add_remaining_traffic=False)
        )
        await session.commit()

    await record_usages._record_user_usages_impl()

    assert await _user_state(recorder_db, user_id) == (UserStatus.active, 0, 5000)
    async with recorder_db() as session:
        remaining_plans = (await session.execute(select(NextPlan.id).where(NextPlan.user_id == user_id))).all()
    assert remaining_plans == []
    assert limit_events["pushed"] == [[user_id]]
    assert limit_events["next_plan"] == [user_id]
    assert limit_events["status"] == []


@pytest.mark.asyncio
async def test_users_below_or_without_a_limit_are_left_alone(
    monkeypatch: pytest.MonkeyPatch, recorder_db, limit_events
):
    _, user_ids, node_ids = await seed_users_and_nodes(recorder_db, user_count=3, node_count=1)
    below, zero_limit, no_limit = user_ids
    await _set_user(recorder_db, below, data_limit=1000, used_traffic=100)
    await _set_user(recorder_db, zero_limit, data_limit=0, used_traffic=5000)
    await _set_user(recorder_db, no_limit, data_limit=None, used_traffic=5000)
    _install(monkeypatch, [CountingNode(node_ids[0], {below: 200, zero_limit: 300, no_limit: 400})])

    await record_usages._record_user_usages_impl()

    assert await _user_state(recorder_db, below) == (UserStatus.active, 300, 1000)
    assert await _user_state(recorder_db, zero_limit) == (UserStatus.active, 5300, 0)
    assert await _user_state(recorder_db, no_limit) == (UserStatus.active, 5400, None)
    assert limit_events == {"pushed": [], "status": [], "next_plan": []}


@pytest.mark.asyncio
async def test_a_failed_limit_push_keeps_the_billing_and_the_tick(
    monkeypatch: pytest.MonkeyPatch, recorder_db, limit_events, caplog
):
    user_id = await _one_user_over_the_line(monkeypatch, recorder_db)
    monkeypatch.setattr(
        record_usages, "enforce_user_limits_now", AsyncMock(side_effect=RuntimeError("limit push unavailable"))
    )
    caplog.set_level(logging.ERROR, logger="record-usages")

    await record_usages._record_user_usages_impl()

    assert await _user_state(recorder_db, user_id) == (UserStatus.active, 1100, 1000)
    _, charted = await usage_snapshot(recorder_db, [user_id])
    assert charted == {user_id: 200}
    assert any(
        record.levelno == logging.ERROR
        and record.exc_info is not None
        and "limit push unavailable" in str(record.exc_info[1])
        for record in caplog.records
    )
    assert limit_events == {"pushed": [], "status": [], "next_plan": []}


@pytest.mark.asyncio
async def test_kept_usage_flushed_at_shutdown_limits_the_user_it_bills(recorder_db, limit_events):
    _, user_ids, node_ids = await seed_users_and_nodes(recorder_db, user_count=1, node_count=1)
    user_id = user_ids[0]
    await _set_user(recorder_db, user_id, data_limit=1000, used_traffic=900)
    record_usages._retained_cohorts.append(
        record_usages.UsageCohort({node_ids[0]: [{"uid": user_id, "value": 200}]}, {node_ids[0]: 1.0}, {user_id: 0})
    )

    await record_usages._flush_retained_cohorts()

    assert await _user_state(recorder_db, user_id) == (UserStatus.limited, 1100, 1000)
    assert limit_events["status"] == [(user_id, UserStatus.limited)]
    assert record_usages._retained_cohorts == []
