from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from app.db.models import User, UserStatus
from app.fork.jobs import status_review
from app.jobs import record_usages, review_users
from app.models.settings import General, OnHoldTimeoutAction
from tests.test_recorder_harness import recorder_db_fixture, seed_users_and_nodes  # noqa: F401


@pytest.fixture(name="hold_events")
def hold_events_fixture(monkeypatch: pytest.MonkeyPatch, recorder_db) -> dict[str, list]:
    events: dict[str, list] = {"pushed": [], "node_removed": [], "row_at_node_removal": [], "fail_node": []}

    async def record_push(users):
        events["pushed"].append([(user.id, user.status) for user in users])

    async def record_status_change(db_user, status):
        return None

    async def record_node_removal(user):
        if events["fail_node"]:
            raise RuntimeError("node down")
        async with recorder_db() as session:
            events["row_at_node_removal"].append(
                (await session.execute(select(User.id).where(User.id == user.id))).scalar_one_or_none()
            )
        events["node_removed"].append(user.id)

    monkeypatch.setattr(review_users, "sync_users", record_push)
    monkeypatch.setattr(review_users, "_notify_status_change", record_status_change)
    monkeypatch.setattr(review_users, "sync_remove_user_awaited", record_node_removal)
    monkeypatch.setattr(review_users.notification, "remove_user", AsyncMock())
    monkeypatch.setattr(review_users, "GetDB", record_usages.GetDB)
    monkeypatch.setattr(status_review, "GetDB", record_usages.GetDB)
    return events


def _use_action(monkeypatch: pytest.MonkeyPatch, action: OnHoldTimeoutAction) -> None:
    monkeypatch.setattr(
        review_users, "general_settings", AsyncMock(return_value=General(on_hold_timeout_action=action))
    )


async def _seed_on_hold(recorder_db, *, came_online: bool) -> int:
    _, user_ids, _ = await seed_users_and_nodes(recorder_db, user_count=1, node_count=0)
    user_id = user_ids[0]
    now = datetime.now(UTC)
    values = {
        "status": UserStatus.on_hold,
        "expire": None,
        "on_hold_expire_duration": 86400,
    }
    if came_online:
        values.update(online_at=now + timedelta(seconds=5), on_hold_timeout=now + timedelta(days=3))
    else:
        values.update(online_at=None, on_hold_timeout=now - timedelta(hours=1))
    async with recorder_db() as session:
        await session.execute(update(User).where(User.id == user_id).values(**values))
        await session.commit()
    return user_id


async def _status(recorder_db, user_id: int) -> UserStatus | None:
    async with recorder_db() as session:
        return (await session.execute(select(User.status).where(User.id == user_id))).scalar_one_or_none()


@pytest.mark.parametrize("action", [OnHoldTimeoutAction.disable, OnHoldTimeoutAction.delete])
@pytest.mark.asyncio
async def test_a_user_that_came_online_is_activated_whatever_the_timeout_action(
    monkeypatch: pytest.MonkeyPatch, recorder_db, hold_events, action
):
    _use_action(monkeypatch, action)
    user_id = await _seed_on_hold(recorder_db, came_online=True)

    await review_users.on_hold_to_active_users_job()

    assert await _status(recorder_db, user_id) == UserStatus.active
    assert hold_events["node_removed"] == []
    assert hold_events["pushed"] == [[(user_id, UserStatus.active)]]


@pytest.mark.asyncio
async def test_disable_action_disables_a_timed_out_user_and_pushes_it(
    monkeypatch: pytest.MonkeyPatch, recorder_db, hold_events
):
    _use_action(monkeypatch, OnHoldTimeoutAction.disable)
    user_id = await _seed_on_hold(recorder_db, came_online=False)

    await review_users.on_hold_to_active_users_job()

    assert await _status(recorder_db, user_id) == UserStatus.disabled
    assert hold_events["pushed"] == [[(user_id, UserStatus.disabled)]]


@pytest.mark.asyncio
async def test_delete_action_removes_a_timed_out_user_from_the_nodes_before_the_row(
    monkeypatch: pytest.MonkeyPatch, recorder_db, hold_events
):
    _use_action(monkeypatch, OnHoldTimeoutAction.delete)
    user_id = await _seed_on_hold(recorder_db, came_online=False)

    await review_users.on_hold_to_active_users_job()

    assert hold_events["node_removed"] == [user_id]
    assert hold_events["row_at_node_removal"] == [user_id]
    assert await _status(recorder_db, user_id) is None


@pytest.mark.asyncio
async def test_delete_action_keeps_the_row_when_the_node_removal_fails(
    monkeypatch: pytest.MonkeyPatch, recorder_db, hold_events
):
    _use_action(monkeypatch, OnHoldTimeoutAction.delete)
    user_id = await _seed_on_hold(recorder_db, came_online=False)
    hold_events["fail_node"].append(True)

    with pytest.raises(RuntimeError):
        await review_users.on_hold_to_active_users_job()

    assert await _status(recorder_db, user_id) == UserStatus.on_hold


@pytest.mark.parametrize(
    "action", [OnHoldTimeoutAction.activate, OnHoldTimeoutAction.disable, OnHoldTimeoutAction.delete]
)
@pytest.mark.asyncio
async def test_no_action_overwrites_an_admin_edit_committed_after_the_read(
    monkeypatch: pytest.MonkeyPatch, recorder_db, hold_events, action
):
    _use_action(monkeypatch, action)
    user_id = await _seed_on_hold(recorder_db, came_online=False)
    real_candidates = review_users.get_on_hold_to_active_users

    async def candidates_then_admin_extends_the_hold(db):
        stale = await real_candidates(db)
        async with recorder_db() as admin_session:
            await admin_session.execute(
                update(User)
                .where(User.id == user_id)
                .values(on_hold_timeout=datetime.now(UTC) + timedelta(days=7), edit_at=datetime.now(UTC))
            )
            await admin_session.commit()
        return stale

    monkeypatch.setattr(review_users, "get_on_hold_to_active_users", candidates_then_admin_extends_the_hold)

    await review_users.on_hold_to_active_users_job()

    assert await _status(recorder_db, user_id) == UserStatus.on_hold
    assert hold_events["node_removed"] == []
    assert hold_events["pushed"] == []


@pytest.mark.parametrize("action", [OnHoldTimeoutAction.disable, OnHoldTimeoutAction.delete])
@pytest.mark.asyncio
async def test_a_first_connection_billed_after_the_read_activates_instead_of_the_timeout_action(
    monkeypatch: pytest.MonkeyPatch, recorder_db, hold_events, action
):
    _use_action(monkeypatch, action)
    user_id = await _seed_on_hold(recorder_db, came_online=False)
    real_candidates = review_users.get_on_hold_to_active_users

    async def candidates_then_the_user_connects(db):
        stale = await real_candidates(db)
        async with recorder_db() as recorder_session:
            await recorder_session.execute(
                update(User)
                .where(User.id == user_id)
                .values(used_traffic=User.used_traffic + 1024, online_at=datetime.now(UTC) + timedelta(seconds=5))
            )
            await recorder_session.commit()
        return stale

    monkeypatch.setattr(review_users, "get_on_hold_to_active_users", candidates_then_the_user_connects)

    await review_users.on_hold_to_active_users_job()

    assert await _status(recorder_db, user_id) == UserStatus.active
    assert hold_events["node_removed"] == []
