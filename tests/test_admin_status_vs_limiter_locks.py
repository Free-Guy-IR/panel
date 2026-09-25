from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app import notification
from app.db.crud.user import get_user_by_id
from app.db.models import User, UserStatus
from app.fork.jobs import connection_limiter
from app.fork.models.connection import ConnectionRestriction
from app.models.settings import ConnectionLimit
from app.models.user import UserStatusToggle
from app.operation import OperatorType, user as user_module
from app.operation.user import UserOperation
from tests.test_recorder_harness import _database_url, recorder_db_fixture, seed_users_and_nodes  # noqa: F401

pytestmark = pytest.mark.skipif(
    _database_url().startswith("sqlite"), reason="needs a server database with row locks and deadlock detection"
)

OWNER = SimpleNamespace(username="owner", is_owner=True, role=None, id=None)
SETTINGS = ConnectionLimit(
    enabled=True, monitor_only=False, enforcement_enabled=True, persistence_cycles=1, punishment_steps=[30]
)


@pytest.fixture(autouse=True)
def quiet(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(user_module, "sync_user", AsyncMock())
    monkeypatch.setattr(connection_limiter, "_push_to_nodes", AsyncMock())
    monkeypatch.setattr(UserOperation, "generate_subscription_url", staticmethod(AsyncMock(return_value="")))
    monkeypatch.setattr(notification, "user_status_change", AsyncMock())


async def _deadlocks() -> int:
    engine = create_async_engine(_database_url(), poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SET GLOBAL innodb_monitor_enable = 'lock_deadlocks'"))
            return int(
                (
                    await conn.execute(
                        text("SELECT `COUNT` FROM information_schema.INNODB_METRICS WHERE NAME = 'lock_deadlocks'")
                    )
                ).scalar_one()
            )
    finally:
        await engine.dispose()


async def _state(recorder_db, user_id: int) -> tuple[UserStatus, int]:
    async with recorder_db() as session:
        status = (await session.execute(select(User.status).where(User.id == user_id))).scalar_one()
        active = len(
            (
                await session.execute(
                    select(ConnectionRestriction.id).where(
                        ConnectionRestriction.user_id == user_id, ConnectionRestriction.active.is_(True)
                    )
                )
            )
            .scalars()
            .all()
        )
    return status, active


async def _admin_disable_while(
    recorder_db, monkeypatch, user_id: int, limiter_step, label: str
) -> tuple[list[str], int]:
    before = await _deadlocks()
    real_close = user_module.close_active_restriction
    events: list[str] = []
    started: list[asyncio.Task] = []

    async def close_after_the_limiter_starts(db, target_id, reason):
        if not started:
            started.append(asyncio.create_task(limiter_step()))
            await asyncio.sleep(1.5)
        return await real_close(db, target_id, reason)

    monkeypatch.setattr(user_module, "close_active_restriction", close_after_the_limiter_starts)
    operation = UserOperation(operator_type=OperatorType.API)
    for attempt in range(2):
        try:
            async with recorder_db() as db:
                db_user = await get_user_by_id(db, user_id, load_admin_role=True)
                await operation._set_user_disabled(db, db_user, UserStatusToggle(disabled=True), OWNER)
            events.append(f"admin committed on attempt {attempt + 1}")
            break
        except HTTPException as exc:
            events.append(f"admin answered {exc.status_code} on attempt {attempt + 1}")
    try:
        await asyncio.wait_for(started[0], timeout=60)
        events.append("limiter committed")
    except Exception as exc:
        events.append(f"limiter aborted: {type(exc).__name__}")
    deadlocks = await _deadlocks() - before
    print(f"MYSQL-PROOF {label} lock_deadlocks={deadlocks} events={events}")
    return events, deadlocks


@pytest.mark.asyncio
async def test_an_admin_disable_overlapping_the_limiter_release_keeps_the_admin_decision(
    monkeypatch: pytest.MonkeyPatch, recorder_db
):
    _, user_ids, _ = await seed_users_and_nodes(recorder_db, user_count=1, node_count=0)
    user_id = user_ids[0]
    async with recorder_db() as session:
        await session.execute(update(User).where(User.id == user_id).values(status=UserStatus.disabled))
        session.add(
            ConnectionRestriction(
                user_id=user_id,
                previous_status=UserStatus.active.value,
                disable_minutes=30,
                restore_at=datetime.now(UTC) - timedelta(seconds=1),
                active=True,
            )
        )
        await session.commit()

    async def limiter_release():
        async with recorder_db() as limiter_db:
            await connection_limiter._release_due(limiter_db, SETTINGS)

    events, _deadlocks_seen = await _admin_disable_while(
        recorder_db, monkeypatch, user_id, limiter_release, "admin-disable-vs-release"
    )
    async with recorder_db() as limiter_db:
        await connection_limiter._release_due(limiter_db, SETTINGS)

    assert await _state(recorder_db, user_id) == (UserStatus.disabled, 0), events
    assert any(event.startswith("admin committed") for event in events), events


@pytest.mark.asyncio
async def test_an_admin_disable_overlapping_the_limiter_enforcement_keeps_the_admin_decision(
    monkeypatch: pytest.MonkeyPatch, recorder_db
):
    _, user_ids, _ = await seed_users_and_nodes(recorder_db, user_count=1, node_count=0)
    user_id = user_ids[0]
    over_limit = SimpleNamespace(
        user_id=user_id, verdict="over_limit", streak=5, devices=4, limit_applied=2, details={}, reasons=["lock"]
    )

    async def limiter_enforce():
        async with recorder_db() as limiter_db:
            await connection_limiter._enforce(limiter_db, [over_limit], SETTINGS)

    events, deadlocks = await _admin_disable_while(
        recorder_db, monkeypatch, user_id, limiter_enforce, "admin-disable-vs-enforce"
    )

    assert (deadlocks, events) == (0, ["admin committed on attempt 1", "limiter committed"]), events
    assert await _state(recorder_db, user_id) == (UserStatus.disabled, 0), events
