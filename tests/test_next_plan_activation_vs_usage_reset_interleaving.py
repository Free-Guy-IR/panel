from __future__ import annotations

import asyncio
import itertools
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, update

from app import notification
from app.db.crud.user import get_user_by_id
from app.db.models import NextPlan, User, UserStatus, UserUsageResetLogs
from app.operation import OperatorType, user as user_operation
from app.operation.user import UserOperation
from tests.test_next_plan_reset_lock_order import server_database_only
from tests.test_recorder_harness import recorder_db_fixture, seed_users_and_nodes  # noqa: F401

OWNER = SimpleNamespace(username="owner", is_owner=True, role=None, id=None)
MB = 1024**2
START_LIMIT = 1000 * MB
START_USED = 400 * MB
PLAN_LIMIT = 5000 * MB
APPLIED_LIMIT = PLAN_LIMIT + START_LIMIT - START_USED


@pytest.fixture(autouse=True)
def quiet(monkeypatch: pytest.MonkeyPatch, recorder_db):
    async def updated(self, db_user):
        return SimpleNamespace(id=db_user.id, username=db_user.username, status=db_user.status)

    monkeypatch.setattr(UserOperation, "update_user", updated)
    monkeypatch.setattr(user_operation, "sync_user", AsyncMock())
    for name in ("user_status_change", "user_data_reset_by_next", "reset_user_data_usage"):
        monkeypatch.setattr(notification, name, AsyncMock())


async def _seed(recorder_db) -> int:
    _, (user_id,), _ = await seed_users_and_nodes(recorder_db, user_count=1, node_count=0)
    async with recorder_db() as session:
        await session.execute(
            update(User)
            .where(User.id == user_id)
            .values(
                status=UserStatus.on_hold,
                on_hold_expire_duration=3600,
                data_limit=START_LIMIT,
                used_traffic=START_USED,
                expire=None,
            )
        )
        session.add(
            NextPlan(
                user_id=user_id,
                user_template_id=None,
                data_limit=PLAN_LIMIT,
                expire=86400 * 30,
                add_remaining_traffic=True,
            )
        )
        await session.commit()
    return user_id


async def _activate(recorder_db, user_id: int) -> str:
    async with recorder_db() as session:
        db_user = await get_user_by_id(session, user_id)
        try:
            await UserOperation(OperatorType.API)._active_next_plan(session, db_user, OWNER)
        except HTTPException as exc:
            if exc.status_code == 404:
                return "no-plan"
            raise
    return "applied"


async def _reset(recorder_db, user_id: int) -> str:
    async with recorder_db() as session:
        db_user = await get_user_by_id(session, user_id)
        await UserOperation(OperatorType.API)._reset_user_data_usage(session, db_user, OWNER)
    return "reset"


async def _state(recorder_db, user_id: int) -> dict:
    async with recorder_db() as session:
        row = (
            await session.execute(
                select(
                    User.status,
                    User.data_limit,
                    User.used_traffic,
                    User.expire,
                    User.on_hold_expire_duration,
                    User.on_hold_timeout,
                ).where(User.id == user_id)
            )
        ).one()
        plans = (await session.execute(select(func.count(NextPlan.id)).where(NextPlan.user_id == user_id))).scalar_one()
        resets = (
            await session.execute(
                select(func.count(UserUsageResetLogs.id)).where(UserUsageResetLogs.user_id == user_id)
            )
        ).scalar_one()
    return dict(row._mapping) | {"plans": plans, "resets": resets}


def _assert_serial_outcome(state: dict, outcomes: list[str], started: datetime) -> None:
    assert state["used_traffic"] == 0, state
    assert state["plans"] == 0, state
    assert state["resets"] == (2 if "applied" in outcomes else 1), (state, outcomes)
    if "applied" in outcomes:
        assert state["status"] == UserStatus.active, state
        assert state["data_limit"] == APPLIED_LIMIT, state
        assert not state["on_hold_expire_duration"] and state["on_hold_timeout"] is None, state
        expire = state["expire"] if state["expire"].tzinfo else state["expire"].replace(tzinfo=UTC)
        assert timedelta(days=30, minutes=-2) < expire - started < timedelta(days=30, minutes=2), state
    else:
        assert state["status"] == UserStatus.on_hold, state
        assert state["data_limit"] == START_LIMIT, state
        assert state["on_hold_expire_duration"] == 3600 and state["expire"] is None, state


STEPS = ("activate", "reset")


@pytest.mark.asyncio
@pytest.mark.parametrize("order", list(itertools.permutations(STEPS)), ids="-".join)
async def test_next_plan_activation_and_usage_reset_in_either_order_end_in_a_serial_outcome(recorder_db, order):
    user_id = await _seed(recorder_db)
    started = datetime.now(UTC)
    steps = {"activate": _activate, "reset": _reset}

    outcomes = [await steps[step](recorder_db, user_id) for step in order]

    _assert_serial_outcome(await _state(recorder_db, user_id), outcomes, started)
    if order[0] == "reset":
        assert outcomes == ["reset", "no-plan"], outcomes


@server_database_only
@pytest.mark.asyncio
@pytest.mark.parametrize("attempt", range(3))
async def test_next_plan_activation_and_usage_reset_started_together_end_in_a_serial_outcome(recorder_db, attempt):
    user_id = await _seed(recorder_db)
    started = datetime.now(UTC)

    outcomes = await asyncio.wait_for(
        asyncio.gather(_activate(recorder_db, user_id), _reset(recorder_db, user_id), return_exceptions=True),
        timeout=90,
    )

    assert not [repr(outcome) for outcome in outcomes if isinstance(outcome, BaseException)], outcomes
    _assert_serial_outcome(await _state(recorder_db, user_id), list(outcomes), started)


@server_database_only
@pytest.mark.asyncio
async def test_a_usage_reset_arriving_while_the_activation_holds_the_user_waits_and_cannot_revive_usage(
    monkeypatch, recorder_db
):
    user_id = await _seed(recorder_db)
    started = datetime.now(UTC)
    inside = asyncio.Event()
    proceed = asyncio.Event()
    real_reset_by_next = user_operation.reset_user_by_next

    async def hold_the_activation(db, db_user, **kwargs):
        inside.set()
        await proceed.wait()
        return await real_reset_by_next(db, db_user, **kwargs)

    monkeypatch.setattr(user_operation, "reset_user_by_next", hold_the_activation)
    activation = asyncio.create_task(_activate(recorder_db, user_id))
    await asyncio.wait_for(inside.wait(), timeout=30)
    reset = asyncio.create_task(_reset(recorder_db, user_id))
    await asyncio.sleep(1.5)
    proceed.set()
    outcomes = await asyncio.wait_for(asyncio.gather(activation, reset, return_exceptions=True), timeout=90)

    assert not [repr(outcome) for outcome in outcomes if isinstance(outcome, BaseException)], outcomes
    assert outcomes == ["applied", "reset"], outcomes
    _assert_serial_outcome(await _state(recorder_db, user_id), list(outcomes), started)
