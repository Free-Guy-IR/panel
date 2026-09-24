import asyncio
import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.models import Admin, AdminRole, Base, User, UserStatus
from app.fork.jobs import connection_limiter as job
from app.jobs import record_usages
from app.models.proxy import ProxyTable
from app.models.settings import ConnectionLimit

MYSQL_URL = os.getenv("TEST_MYSQL_URL", "")

pytestmark = pytest.mark.skipif(not MYSQL_URL.startswith("mysql"), reason="TEST_MYSQL_URL is not set")


def _observation(user_id: int):
    return SimpleNamespace(
        user_id=user_id,
        devices=1,
        address_sources=1,
        hwid_count=0,
        node_count=1,
        app_count=1,
        verdict="within_limit",
        limit_applied=2,
        node_streak=0,
        at_once_streak=0,
        streak=0,
        reasons=[],
        details={},
    )


async def _fresh_schema(user_count: int) -> list[int]:
    engine = create_async_engine(MYSQL_URL, poolclass=NullPool)
    json_column = Base.metadata.tables["users"].c.proxy_settings
    json_default = json_column.server_default
    json_column.server_default = None
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as db:
            role = AdminRole(name="operator", is_owner=False, permissions={}, limits={}, features={}, access={})
            db.add(role)
            await db.flush()
            admin = Admin(username="owner", hashed_password="x", role_id=role.id)
            db.add(admin)
            await db.flush()
            users = [
                User(username=f"user-{index}", proxy_settings=ProxyTable().dict(no_obj=True), admin_id=admin.id)
                for index in range(user_count)
            ]
            for user in users:
                user.status = UserStatus.active
            db.add_all(users)
            await db.commit()
            return [user.id for user in users]
    finally:
        json_column.server_default = json_default
        await engine.dispose()


async def _deadlocks() -> int:
    engine = create_async_engine(MYSQL_URL, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            row = (await conn.execute(text("SHOW GLOBAL STATUS LIKE 'Innodb_deadlocks'"))).one()
            return int(row[1])
    finally:
        await engine.dispose()


def _recorder_in_thread(user_ids: list[int], start: threading.Event, outcome: dict) -> threading.Thread:
    def run():
        async def bill():
            engine = create_async_engine(MYSQL_URL, poolclass=NullPool)
            record_usages.engine = engine
            record_usages._dialect_cache[:] = ["mysql"]
            try:
                started = time.monotonic()
                await record_usages.apply_fenced_user_usage(
                    [{"uid": user_id, "value": 100} for user_id in user_ids],
                    {user_id: record_usages.UserUsageContext(None, 0) for user_id in user_ids},
                    {user_id: 0 for user_id in user_ids},
                    [],
                )
                outcome["recorder_waited_s"] = time.monotonic() - started
            except Exception as exc:
                outcome["recorder_error"] = repr(exc)
            finally:
                await engine.dispose()

        start.wait(10)
        asyncio.run(bill())

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


@pytest.mark.asyncio
async def test_connection_state_writes_do_not_deadlock_with_the_recorder_on_mysql(monkeypatch):
    monkeypatch.setattr(record_usages, "engine", record_usages.engine)
    monkeypatch.setattr(record_usages, "_dialect_cache", [])
    user_ids = await _fresh_schema(2)
    before = await _deadlocks()
    outcome: dict = {}
    start = threading.Event()
    engine = create_async_engine(MYSQL_URL, poolclass=NullPool)
    paused = {"done": False}

    def pause_after_first_insert(conn, cursor, statement, parameters, context, executemany):
        if not paused["done"] and statement.lstrip().upper().startswith("INSERT INTO USER_CONNECTION_STATES"):
            paused["done"] = True
            start.set()
            time.sleep(1.5)

    event.listen(engine.sync_engine, "after_cursor_execute", pause_after_first_insert)
    thread = _recorder_in_thread(user_ids, start, outcome)
    state_error = None
    try:
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as db:
            try:
                await job._write_states(db, [_observation(user_id) for user_id in sorted(user_ids, reverse=True)])
            except Exception as exc:
                state_error = repr(exc)
                await db.rollback()
    finally:
        thread.join(30)
        await engine.dispose()

    deadlocks = await _deadlocks() - before
    print(f"MYSQL-PROOF a2-usage-12 deadlocks={deadlocks} state_error={state_error} recorder={outcome}")
    assert paused["done"]
    assert "recorder_error" not in outcome
    assert (deadlocks, state_error) == (0, None)


@pytest.mark.asyncio
async def test_a_prune_that_deleted_nothing_does_not_hold_user_locks_through_the_assessment(monkeypatch):
    monkeypatch.setattr(record_usages, "engine", record_usages.engine)
    monkeypatch.setattr(record_usages, "_dialect_cache", [])
    user_ids = await _fresh_schema(3)
    engine = create_async_engine(MYSQL_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    outcome: dict = {}
    start = threading.Event()
    settings = ConnectionLimit(enabled=True, enforcement_enabled=False, check_interval_seconds=30)

    class _GetDB:
        async def __aenter__(self):
            self.db = factory()
            return self.db

        async def __aexit__(self, *exc):
            await self.db.close()

    async def slow_assessment(db, _settings):
        start.set()
        await asyncio.sleep(3.0)
        return []

    monkeypatch.setattr(job, "GetDB", _GetDB)
    monkeypatch.setattr(job, "_current_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(job, "_release_due", AsyncMock())
    monkeypatch.setattr(job, "_last_assessment", 0.0)
    monkeypatch.setattr(job, "_seconds_since_last_check", AsyncMock(return_value=None))
    monkeypatch.setattr(job, "run_assessment", slow_assessment)

    thread = _recorder_in_thread(user_ids, start, outcome)
    try:
        await job.record_connection_states()
    finally:
        thread.join(30)
        await engine.dispose()

    print(f"MYSQL-PROOF a2-usage-12 latent recorder={outcome}")
    assert "recorder_error" not in outcome
    assert outcome["recorder_waited_s"] < 1.0, outcome
