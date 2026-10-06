from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select, text, update
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool
from sqlalchemy.sql.util import find_tables

import app.settings as settings_module
from app import notification
from app.db.crud.admin import get_admin_by_id
from app.db.crud.user import get_user_by_id
from app.db.models import User, UserStatus, UserTemplate, UserUsageResetLogs
from app.fork.jobs import connection_limiter
from app.fork.models.connection import ConnectionRestriction
from app.fork.proxy_secrets import locks as locks_module
from app.fork.proxy_secrets.locks import LOCK_UNAVAILABLE_DETAIL, PROXY_SECRETS_LOCK
from app.models.admin import AdminDetails, AdminRoleData
from app.models.settings import ConnectionLimit
from app.models.user import ModifyUserByTemplate
from app.operation import OperatorType, admin as admin_operation, user as user_operation
from app.operation.admin import AdminOperation
from app.operation.user import UserOperation
from app.utils import connection_enforcement
from tests.test_recorder_harness import _database_url, recorder_db_fixture, seed_users_and_nodes  # noqa: F401

OWNER = AdminDetails(id=None, username="owner", role=AdminRoleData(id=1, name="owner", is_owner=True))
SETTINGS = ConnectionLimit(
    enabled=True, monitor_only=False, enforcement_enabled=True, persistence_cycles=1, punishment_steps=[30]
)
ADMIN_WIDE = ["_activate_all_disabled_users_for_admin", "_disable_all_active_users_for_admin"]
TEMPLATE_DATA_LIMIT = 10**9
SEEDED_USED_TRAFFIC = 1234
SEEDED_DATA_LIMIT = 5000
server_database_only = pytest.mark.skipif(
    _database_url().startswith("sqlite"), reason="needs a server database with row locks and deadlock detection"
)


class _DriverError(Exception):
    pass


def _deadlock() -> OperationalError:
    return OperationalError(
        "UPDATE users", {}, _DriverError(1213, "Deadlock found when trying to get lock; try restarting transaction")
    )


def _lock_wait_timeout() -> OperationalError:
    return OperationalError(
        "UPDATE users", {}, _DriverError(1205, "Lock wait timeout exceeded; try restarting transaction")
    )


@pytest.fixture(autouse=True)
async def quiet(monkeypatch: pytest.MonkeyPatch):
    stored = SimpleNamespace(
        telegram={}, webhook={}, notification_settings={}, notification_enable={}, subscription={}, hwid={}, general={}
    )
    monkeypatch.setattr(settings_module, "stored_settings", AsyncMock(return_value=stored))
    await settings_module.refresh_caches()
    monkeypatch.setattr(connection_limiter, "_push_to_nodes", AsyncMock())
    monkeypatch.setattr(admin_operation, "sync_users", AsyncMock())
    monkeypatch.setattr(user_operation, "sync_user", AsyncMock())
    monkeypatch.setattr(UserOperation, "generate_subscription_url", staticmethod(AsyncMock(return_value="")))
    for name in ("user_status_change", "modify_user", "reset_user_data_usage"):
        monkeypatch.setattr(notification, name, AsyncMock())
    yield
    await settings_module.refresh_caches()


async def _restricted_user(recorder_db) -> tuple[int, int]:
    admin_id, (user_id,), _ = await seed_users_and_nodes(recorder_db, user_count=1, node_count=0)
    async with recorder_db() as session:
        await session.execute(
            update(User)
            .where(User.id == user_id)
            .values(status=UserStatus.disabled, used_traffic=SEEDED_USED_TRAFFIC, data_limit=SEEDED_DATA_LIMIT)
        )
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
    return admin_id, user_id


async def _template(recorder_db, *, reset_usages: bool) -> int:
    async with recorder_db() as session:
        template = UserTemplate(
            name=f"renewal-{uuid4().hex[:10]}",
            username_prefix=None,
            username_suffix=None,
            extra_settings=None,
            groups=[],
            data_limit=TEMPLATE_DATA_LIMIT,
            expire_duration=86400,
            reset_usages=reset_usages,
        )
        session.add(template)
        await session.commit()
        return template.id


async def _apply_template(recorder_db, user_id: int, template_id: int) -> None:
    async with recorder_db() as db:
        db_user = await get_user_by_id(db, user_id, load_admin_role=True)
        await UserOperation(OperatorType.API)._modify_user_with_template(
            db, db_user, ModifyUserByTemplate(user_template_id=template_id), OWNER
        )


async def _admin_wide(recorder_db, admin_id: int, action: str) -> None:
    async with recorder_db() as db:
        db_admin = await get_admin_by_id(db, admin_id)
        await getattr(AdminOperation(OperatorType.API), action)(db, db_admin, OWNER)


async def _row(recorder_db, user_id: int) -> SimpleNamespace:
    async with recorder_db() as session:
        user = (
            await session.execute(select(User.status, User.used_traffic, User.data_limit).where(User.id == user_id))
        ).one()
        active = (
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
        resets = (
            (await session.execute(select(UserUsageResetLogs.id).where(UserUsageResetLogs.user_id == user_id)))
            .scalars()
            .all()
        )
    return SimpleNamespace(
        status=user.status,
        used_traffic=user.used_traffic,
        data_limit=user.data_limit,
        active_restrictions=len(active),
        resets=len(resets),
    )


def _outcome(label: str, events: list[str]):
    async def run(step) -> None:
        try:
            await step()
            events.append(f"{label} committed")
        except HTTPException as exc:
            events.append(f"{label} answered {exc.status_code}")
        except Exception as exc:
            events.append(f"{label} aborted: {type(exc).__name__}: {getattr(exc, 'orig', exc)!r}"[:300])

    return run


async def _release(recorder_db) -> None:
    async with recorder_db() as db:
        await connection_limiter._release_due(db, SETTINGS)


@pytest.fixture(name="lock_trace")
def lock_trace_fixture():
    trace: list[tuple[str, str]] = []

    def on_orm(state):
        statement = state.statement
        if state.is_select and statement._for_update_arg is not None:
            for table in sorted({t.name for f in statement.get_final_froms() for t in find_tables(f)}):
                trace.append(("lock", table))
        elif state.is_update or state.is_delete or state.is_insert:
            name = statement.table.name
            trace.append(("named lock", name) if name == "named_locks" else ("write", name))

    def on_commit(_session):
        trace.append(("commit", ""))

    def on_rollback(_session):
        trace.append(("rollback", ""))

    event.listen(Session, "do_orm_execute", on_orm)
    event.listen(Session, "after_commit", on_commit)
    event.listen(Session, "after_rollback", on_rollback)
    yield trace
    event.remove(Session, "do_orm_execute", on_orm)
    event.remove(Session, "after_commit", on_commit)
    event.remove(Session, "after_rollback", on_rollback)


def _restriction_writes_without_their_user_row(trace: list[tuple[str, str]], *, named_lock_too: bool) -> list[int]:
    offending = []
    since_commit: list[tuple[str, str]] = []
    for index, entry in enumerate(trace):
        if entry[0] in ("commit", "rollback"):
            since_commit = []
            continue
        if entry == ("write", "connection_restrictions"):
            holds_user = ("lock", "users") in since_commit
            holds_named = ("named lock", "named_locks") in since_commit
            if not holds_user or (named_lock_too and not holds_named):
                offending.append(index)
        since_commit.append(entry)
    return offending


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ADMIN_WIDE)
async def test_an_admin_wide_change_locks_its_users_before_it_closes_their_restrictions(
    recorder_db, lock_trace, action
):
    admin_id, user_id = await _restricted_user(recorder_db)
    lock_trace.clear()

    await _admin_wide(recorder_db, admin_id, action)

    writes = [entry for entry in lock_trace if entry == ("write", "connection_restrictions")]
    assert writes, f"the admin-wide change never closed the restriction: {lock_trace}"
    assert not _restriction_writes_without_their_user_row(lock_trace, named_lock_too=False), lock_trace
    assert (await _row(recorder_db, user_id)).active_restrictions == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [_deadlock, _lock_wait_timeout], ids=["deadlock", "lock-wait-timeout"])
@pytest.mark.parametrize(
    ("action", "crud_name"),
    [
        ("_activate_all_disabled_users_for_admin", "activate_all_disabled_users"),
        ("_disable_all_active_users_for_admin", "disable_all_active_users"),
    ],
)
async def test_an_admin_wide_change_that_loses_a_lock_answers_503_and_stores_nothing(
    monkeypatch, recorder_db, error, action, crud_name
):
    admin_id, user_id = await _restricted_user(recorder_db)

    async def lose_the_lock(*_args, **_kwargs):
        raise error()

    monkeypatch.setattr(admin_operation, crud_name, lose_the_lock)

    with pytest.raises(HTTPException) as answer:
        await _admin_wide(recorder_db, admin_id, action)

    assert (answer.value.status_code, answer.value.detail) == (503, LOCK_UNAVAILABLE_DETAIL)
    kept = await _row(recorder_db, user_id)
    assert (kept.status, kept.active_restrictions) == (UserStatus.disabled, 1)
    admin_operation.sync_users.assert_not_awaited()


@pytest.mark.asyncio
async def test_closing_the_restrictions_of_many_users_goes_in_bounded_batches_and_closes_them_all(
    monkeypatch, recorder_db
):
    _, user_ids, _ = await seed_users_and_nodes(recorder_db, user_count=5, node_count=0)
    async with recorder_db() as session:
        session.add_all(
            ConnectionRestriction(
                user_id=user_id, previous_status=UserStatus.active.value, disable_minutes=30, active=True
            )
            for user_id in user_ids
        )
        await session.commit()
    monkeypatch.setattr(connection_enforcement, "RELEASE_LOCK_BATCH_SIZE", 2)
    batches: list[str] = []

    def on_orm(state):
        if state.is_update and state.statement.table.name == "connection_restrictions":
            batches.append(state.statement.table.name)

    event.listen(Session, "do_orm_execute", on_orm)
    try:
        async with recorder_db() as db:
            closed = await connection_enforcement.close_active_restrictions(db, reversed(user_ids), "test")
            await db.commit()
    finally:
        event.remove(Session, "do_orm_execute", on_orm)

    assert closed == len(user_ids)
    assert len(batches) == 3, batches
    assert [(await _row(recorder_db, user_id)).active_restrictions for user_id in user_ids] == [0] * len(user_ids)


@pytest.mark.asyncio
@pytest.mark.parametrize("reset_usages", [True, False], ids=["reset-usages", "keep-usages"])
async def test_a_template_renewal_closes_the_restriction_only_while_it_holds_the_named_lock_and_the_user_row(
    recorder_db, lock_trace, reset_usages
):
    _, user_id = await _restricted_user(recorder_db)
    template_id = await _template(recorder_db, reset_usages=reset_usages)
    lock_trace.clear()

    await _apply_template(recorder_db, user_id, template_id)

    writes = [entry for entry in lock_trace if entry == ("write", "connection_restrictions")]
    assert writes, f"the template never closed the restriction: {lock_trace}"
    assert not _restriction_writes_without_their_user_row(lock_trace, named_lock_too=True), lock_trace
    after = await _row(recorder_db, user_id)
    assert (after.status, after.data_limit, after.active_restrictions) == (UserStatus.active, TEMPLATE_DATA_LIMIT, 0)
    assert after.used_traffic == (0 if reset_usages else SEEDED_USED_TRAFFIC)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [_deadlock, _lock_wait_timeout], ids=["deadlock", "lock-wait-timeout"])
@pytest.mark.parametrize("reset_usages", [True, False], ids=["reset-usages", "keep-usages"])
async def test_a_template_renewal_that_loses_a_lock_while_applying_answers_503_and_says_what_was_stored(
    monkeypatch, recorder_db, error, reset_usages
):
    _, user_id = await _restricted_user(recorder_db)
    template_id = await _template(recorder_db, reset_usages=reset_usages)
    real_modify = user_operation.crud_modify_user
    raised: list[bool] = []

    async def lose_the_lock(*args, **kwargs):
        if not raised:
            raised.append(True)
            raise error()
        return await real_modify(*args, **kwargs)

    monkeypatch.setattr(user_operation, "crud_modify_user", lose_the_lock)

    with pytest.raises(HTTPException) as answer:
        await _apply_template(recorder_db, user_id, template_id)

    assert raised
    expected = locks_module.TEMPLATE_RESET_KEPT_DETAIL if reset_usages else LOCK_UNAVAILABLE_DETAIL
    assert (answer.value.status_code, answer.value.detail) == (503, expected)
    kept = await _row(recorder_db, user_id)
    assert (kept.status, kept.data_limit, kept.active_restrictions) == (UserStatus.disabled, SEEDED_DATA_LIMIT, 1)
    if reset_usages:
        assert (kept.used_traffic, kept.resets) == (0, 1)
    else:
        assert (kept.used_traffic, kept.resets) == (SEEDED_USED_TRAFFIC, 0)

    await _apply_template(recorder_db, user_id, template_id)
    retried = await _row(recorder_db, user_id)
    assert (retried.status, retried.data_limit, retried.active_restrictions) == (
        UserStatus.active,
        TEMPLATE_DATA_LIMIT,
        0,
    )


@pytest.mark.asyncio
async def test_a_template_renewal_whose_relock_after_the_usage_reset_times_out_answers_503_and_says_the_reset_was_stored(
    monkeypatch, recorder_db
):
    _, user_id = await _restricted_user(recorder_db)
    template_id = await _template(recorder_db, reset_usages=True)
    real_reset = user_operation.reset_user_data_usage
    real_acquire = locks_module.acquire_named_lock
    reset_committed: list[bool] = []
    refused: list[str] = []

    async def reset_then_note(*args, **kwargs):
        result = await real_reset(*args, **kwargs)
        reset_committed.append(True)
        return result

    async def first_take_after_the_reset_times_out(db, name):
        if name == PROXY_SECRETS_LOCK and reset_committed and not refused:
            refused.append(name)
            raise _lock_wait_timeout()
        return await real_acquire(db, name)

    monkeypatch.setattr(user_operation, "reset_user_data_usage", reset_then_note)
    monkeypatch.setattr(locks_module, "acquire_named_lock", first_take_after_the_reset_times_out)

    with pytest.raises(HTTPException) as answer:
        await _apply_template(recorder_db, user_id, template_id)

    assert refused, "the template never took the named lock again after the usage reset committed"
    assert (answer.value.status_code, answer.value.detail) == (503, locks_module.TEMPLATE_RESET_KEPT_DETAIL)
    kept = await _row(recorder_db, user_id)
    assert (kept.used_traffic, kept.resets, kept.data_limit, kept.active_restrictions) == (
        0,
        1,
        SEEDED_DATA_LIMIT,
        1,
    )


async def _nowait(sql: str, **params) -> str:
    url = _database_url()
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            try:
                await conn.execute(text(sql), params)
                return "free"
            except DBAPIError:
                return "held"
            finally:
                await conn.rollback()
    finally:
        await engine.dispose()


@server_database_only
@pytest.mark.asyncio
async def test_after_the_usage_reset_commits_the_template_holds_the_named_lock_and_the_user_row_while_it_closes_the_restriction(
    monkeypatch, recorder_db
):
    _, user_id = await _restricted_user(recorder_db)
    template_id = await _template(recorder_db, reset_usages=True)
    real_close = user_operation.close_active_restriction
    seen: list[tuple[str, str]] = []

    async def probe_then_close(db, closed_id, reason):
        if not seen:
            seen.append(
                (
                    await _nowait("SELECT id FROM users WHERE id = :id FOR UPDATE NOWAIT", id=closed_id),
                    await _nowait(
                        "SELECT name FROM named_locks WHERE name = :name FOR UPDATE NOWAIT", name=PROXY_SECRETS_LOCK
                    ),
                )
            )
        return await real_close(db, closed_id, reason)

    monkeypatch.setattr(user_operation, "close_active_restriction", probe_then_close)

    await _apply_template(recorder_db, user_id, template_id)

    assert seen == [("held", "held")], seen
    after = await _row(recorder_db, user_id)
    assert (after.used_traffic, after.data_limit, after.active_restrictions) == (0, TEMPLATE_DATA_LIMIT, 0)


@server_database_only
@pytest.mark.asyncio
@pytest.mark.parametrize("action", ADMIN_WIDE)
async def test_an_admin_wide_change_arriving_while_the_release_holds_the_user_row_commits_both(
    monkeypatch: pytest.MonkeyPatch, recorder_db, action
):
    admin_id, user_id = await _restricted_user(recorder_db)
    holding = asyncio.Event()
    proceed = asyncio.Event()
    real_lock = connection_enforcement._lock_user_rows

    async def hold_user_rows(db, user_ids):
        locked = await real_lock(db, user_ids)
        holding.set()
        await proceed.wait()
        return locked

    monkeypatch.setattr(connection_enforcement, "_lock_user_rows", hold_user_rows)
    events: list[str] = []
    limiter_task = asyncio.create_task(_outcome("limiter", events)(lambda: _release(recorder_db)))
    await asyncio.wait_for(holding.wait(), timeout=30)
    admin_task = asyncio.create_task(_outcome("admin-wide", events)(lambda: _admin_wide(recorder_db, admin_id, action)))
    await asyncio.sleep(1.5)
    waited = not admin_task.done()
    proceed.set()
    await asyncio.wait_for(asyncio.gather(limiter_task, admin_task), timeout=90)

    assert sorted(events) == ["admin-wide committed", "limiter committed"], events
    assert waited, "the admin-wide change did not wait for the user row the release held"
    after = await _row(recorder_db, user_id)
    expected = UserStatus.active if action == ADMIN_WIDE[0] else UserStatus.disabled
    assert (after.status, after.active_restrictions) == (expected, 0), events


@server_database_only
@pytest.mark.asyncio
@pytest.mark.parametrize("action", ADMIN_WIDE)
async def test_the_release_arriving_after_an_admin_wide_change_closed_the_restrictions_commits_both(
    monkeypatch: pytest.MonkeyPatch, recorder_db, action
):
    admin_id, user_id = await _restricted_user(recorder_db)
    real_close = admin_operation.close_visible_restrictions
    events: list[str] = []
    started: list[asyncio.Task] = []

    async def close_then_let_the_release_in(db, target_admin_id, caller, reason):
        closed = await real_close(db, target_admin_id, caller, reason)
        started.append(asyncio.create_task(_outcome("limiter", events)(lambda: _release(recorder_db))))
        await asyncio.sleep(1.5)
        return closed

    monkeypatch.setattr(admin_operation, "close_visible_restrictions", close_then_let_the_release_in)

    await _outcome("admin-wide", events)(lambda: _admin_wide(recorder_db, admin_id, action))
    assert started, events
    await asyncio.wait_for(started[0], timeout=60)

    assert sorted(events) == ["admin-wide committed", "limiter committed"], events
    after = await _row(recorder_db, user_id)
    expected = UserStatus.active if action == ADMIN_WIDE[0] else UserStatus.disabled
    assert (after.status, after.active_restrictions) == (expected, 0), events


@server_database_only
@pytest.mark.asyncio
async def test_a_template_renewal_with_reset_overlapping_the_release_commits_both(
    monkeypatch: pytest.MonkeyPatch, recorder_db
):
    _, user_id = await _restricted_user(recorder_db)
    template_id = await _template(recorder_db, reset_usages=True)
    real_close = user_operation.close_active_restriction
    events: list[str] = []
    started: list[asyncio.Task] = []

    async def close_then_let_the_release_in(db, closed_id, reason):
        closed = await real_close(db, closed_id, reason)
        if not started:
            started.append(asyncio.create_task(_outcome("limiter", events)(lambda: _release(recorder_db))))
            await asyncio.sleep(1.5)
        return closed

    monkeypatch.setattr(user_operation, "close_active_restriction", close_then_let_the_release_in)

    await _outcome("template renewal", events)(lambda: _apply_template(recorder_db, user_id, template_id))
    assert started, events
    await asyncio.wait_for(started[0], timeout=60)

    assert sorted(events) == ["limiter committed", "template renewal committed"], events
    after = await _row(recorder_db, user_id)
    assert (after.status, after.used_traffic, after.data_limit, after.active_restrictions) == (
        UserStatus.active,
        0,
        TEMPLATE_DATA_LIMIT,
        0,
    ), events


@server_database_only
@pytest.mark.asyncio
@pytest.mark.parametrize("disabled", [True, False], ids=["toggle-disable", "toggle-enable"])
async def test_an_admin_wide_activate_arriving_while_a_single_toggle_holds_a_restricted_user_waits_and_both_commit(
    monkeypatch: pytest.MonkeyPatch, recorder_db, disabled
):
    from app.models.user import UserStatusToggle

    admin_id, user_id = await _restricted_user(recorder_db)
    real_lock = user_operation.lock_user_rows
    events: list[str] = []
    admin_wide: list[asyncio.Task] = []
    finished_while_held: list[bool] = []

    async def hold_the_row_while_the_admin_wide_activate_arrives(db, user_ids):
        await real_lock(db, user_ids)
        if not admin_wide:
            admin_wide.append(
                asyncio.create_task(
                    _outcome("admin-wide", events)(
                        lambda: _admin_wide(recorder_db, admin_id, "_activate_all_disabled_users_for_admin")
                    )
                )
            )
            await asyncio.sleep(1.5)
            finished_while_held.append(admin_wide[0].done())

    monkeypatch.setattr(user_operation, "lock_user_rows", hold_the_row_while_the_admin_wide_activate_arrives)

    async def toggle() -> None:
        async with recorder_db() as db:
            db_user = await get_user_by_id(db, user_id, load_admin_role=True)
            await UserOperation(OperatorType.API)._set_user_disabled(
                db, db_user, UserStatusToggle(disabled=disabled), OWNER
            )

    await _outcome("toggle", events)(toggle)
    assert admin_wide, events
    await asyncio.wait_for(admin_wide[0], timeout=60)

    assert sorted(events) == ["admin-wide committed", "toggle committed"], events
    assert finished_while_held == [False], "the admin-wide activate did not wait for the row the toggle held"
    after = await _row(recorder_db, user_id)
    assert (after.status, after.active_restrictions) == (UserStatus.active, 0), events
