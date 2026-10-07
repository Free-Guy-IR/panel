from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from app.db.models import User, UserStatus
from app.fork.jobs import connection_limiter
from app.fork.models.connection import ConnectionRestriction
from app.models.settings import ConnectionLimit
from tests.test_recorder_harness import recorder_db_fixture, seed_users_and_nodes  # noqa: F401

SETTINGS = ConnectionLimit(
    enabled=True, monitor_only=False, enforcement_enabled=True, persistence_cycles=1, punishment_steps=[30]
)


class _DriverError(Exception):
    def __init__(self, *args, sqlstate: str | None = None):
        super().__init__(*args)
        self.sqlstate = sqlstate


def _mysql_deadlock() -> OperationalError:
    return OperationalError(
        "COMMIT", {}, _DriverError(1213, "Deadlock found when trying to get lock; try restarting transaction")
    )


def _mysql_lock_wait_timeout() -> OperationalError:
    return OperationalError("COMMIT", {}, _DriverError(1205, "Lock wait timeout exceeded; try restarting transaction"))


def _postgres_deadlock() -> OperationalError:
    return OperationalError("COMMIT", {}, _DriverError(sqlstate="40P01"))


def _unrelated() -> OperationalError:
    return OperationalError("COMMIT", {}, _DriverError(1054, "Unknown column 'x' in 'field list'"))


LOCK_ERRORS = pytest.mark.parametrize(
    "lock_error",
    [_mysql_deadlock, _mysql_lock_wait_timeout, _postgres_deadlock],
    ids=["mysql-deadlock", "mysql-lock-wait-timeout", "postgres-deadlock"],
)


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(connection_limiter, "RETRY_BACKOFF_SECONDS", 0)


@pytest.fixture(name="pushed")
def pushed_fixture(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    pushed: list[int] = []

    async def record(users):
        pushed.extend(user.id for user in users)

    monkeypatch.setattr(connection_limiter, "_push_to_nodes", record)
    return pushed


@pytest.fixture(name="warnings")
def warnings_fixture(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    def record(message, *args, **_kwargs):
        seen.append(message % args)

    monkeypatch.setattr(connection_limiter.logger, "warning", record)
    return seen


def _fail_commits(db, failures: int, make_error, between=None) -> SimpleNamespace:
    real_commit, real_rollback = db.commit, db.rollback
    state = SimpleNamespace(commits=0, failed=0, landed=0, pending=False)

    async def commit():
        state.commits += 1
        if state.failed < failures:
            state.failed += 1
            state.pending = True
            raise make_error()
        await real_commit()

    async def rollback():
        await real_rollback()
        if state.pending:
            state.pending = False
            if between is not None:
                await between()
                state.landed += 1

    db.commit = commit
    db.rollback = rollback
    return state


async def _restricted_user(recorder_db) -> int:
    _, (user_id,), _ = await seed_users_and_nodes(recorder_db, user_count=1, node_count=0)
    async with recorder_db() as session:
        await session.execute(
            update(User)
            .where(User.id == user_id)
            .values(status=UserStatus.disabled, last_status_change=datetime.now(UTC) - timedelta(minutes=40))
        )
        session.add(
            ConnectionRestriction(
                user_id=user_id,
                created_at=datetime.now(UTC) - timedelta(minutes=30),
                previous_status=UserStatus.active.value,
                disable_minutes=30,
                restore_at=datetime.now(UTC) - timedelta(seconds=1),
                active=True,
            )
        )
        await session.commit()
    return user_id


async def _active_user(recorder_db) -> int:
    _, (user_id,), _ = await seed_users_and_nodes(recorder_db, user_count=1, node_count=0)
    return user_id


async def _state(recorder_db, user_id: int) -> tuple[UserStatus, int, int]:
    async with recorder_db() as session:
        status = (await session.execute(select(User.status).where(User.id == user_id))).scalar_one()
        restrictions = (
            (
                await session.execute(
                    select(ConnectionRestriction.active).where(ConnectionRestriction.user_id == user_id)
                )
            )
            .scalars()
            .all()
        )
    return status, sum(1 for active in restrictions if active), len(restrictions)


def _observation(user_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        user_id=user_id, verdict="over_limit", streak=5, devices=4, limit_applied=2, details={}, reasons=["retry"]
    )


@pytest.mark.asyncio
@LOCK_ERRORS
async def test_a_release_that_hits_a_lock_error_is_retried_and_releases(recorder_db, pushed, warnings, lock_error):
    user_id = await _restricted_user(recorder_db)

    async with recorder_db() as db:
        state = _fail_commits(db, 1, lock_error)
        await connection_limiter._release_due(db, SETTINGS)

    assert state.failed == 1
    assert await _state(recorder_db, user_id) == (UserStatus.active, 0, 1)
    assert pushed == [user_id]
    assert warnings == []


@pytest.mark.asyncio
async def test_a_release_retried_after_a_deadlock_rereads_and_respects_the_admin_decision_that_landed_in_between(
    recorder_db, pushed
):
    user_id = await _restricted_user(recorder_db)

    async def admin_disables_and_closes_meanwhile():
        async with recorder_db() as other:
            await other.execute(
                update(ConnectionRestriction)
                .where(ConnectionRestriction.user_id == user_id, ConnectionRestriction.active.is_(True))
                .values(active=False, restored_at=datetime.now(UTC))
            )
            await other.execute(
                update(User)
                .where(User.id == user_id)
                .values(status=UserStatus.disabled, last_status_change=datetime.now(UTC))
            )
            await other.commit()

    async with recorder_db() as db:
        state = _fail_commits(db, 1, _mysql_deadlock, admin_disables_and_closes_meanwhile)
        await connection_limiter._release_due(db, SETTINGS)

    assert (state.failed, state.landed) == (1, 1)
    assert await _state(recorder_db, user_id) == (UserStatus.disabled, 0, 1), (
        "the retry replayed the release decided before the deadlock and undid the admin's disable"
    )
    assert pushed == []


@pytest.mark.asyncio
async def test_a_release_that_keeps_hitting_lock_errors_is_postponed_with_a_warning_and_the_next_run_releases(
    recorder_db, pushed, warnings
):
    user_id = await _restricted_user(recorder_db)

    async with recorder_db() as db:
        state = _fail_commits(db, connection_limiter.RETRIABLE_WRITE_ATTEMPTS, _mysql_deadlock)
        await connection_limiter._release_due(db, SETTINGS)

    assert state.failed == connection_limiter.RETRIABLE_WRITE_ATTEMPTS
    assert await _state(recorder_db, user_id) == (UserStatus.disabled, 1, 1)
    assert pushed == []
    assert len(warnings) == 1 and "next run retries it" in warnings[0], warnings

    async with recorder_db() as db:
        await connection_limiter._release_due(db, SETTINGS)

    assert await _state(recorder_db, user_id) == (UserStatus.active, 0, 1)
    assert pushed == [user_id]


@pytest.mark.asyncio
async def test_a_release_that_fails_for_another_reason_raises_without_a_retry(recorder_db, pushed, warnings):
    user_id = await _restricted_user(recorder_db)

    async with recorder_db() as db:
        state = _fail_commits(db, 1, _unrelated)
        with pytest.raises(OperationalError):
            await connection_limiter._release_due(db, SETTINGS)

    assert state.commits == 1
    assert await _state(recorder_db, user_id) == (UserStatus.disabled, 1, 1)
    assert (pushed, warnings) == ([], [])


@pytest.mark.asyncio
@LOCK_ERRORS
async def test_an_enforcement_that_hits_a_lock_error_is_retried_and_restricts(
    recorder_db, pushed, warnings, lock_error
):
    user_id = await _active_user(recorder_db)

    async with recorder_db() as db:
        state = _fail_commits(db, 1, lock_error)
        await connection_limiter._enforce(db, [_observation(user_id)], SETTINGS)

    assert state.failed == 1
    assert await _state(recorder_db, user_id) == (UserStatus.disabled, 1, 1)
    assert pushed == [user_id]
    assert warnings == []


async def _admin_disabled(recorder_db, user_id: int) -> None:
    async with recorder_db() as other:
        await other.execute(
            update(User)
            .where(User.id == user_id)
            .values(status=UserStatus.disabled, last_status_change=datetime.now(UTC))
        )
        await other.commit()


async def _restricted_elsewhere(recorder_db, user_id: int) -> None:
    async with recorder_db() as other:
        await other.execute(
            update(User)
            .where(User.id == user_id)
            .values(status=UserStatus.disabled, last_status_change=datetime.now(UTC))
        )
        other.add(
            ConnectionRestriction(
                user_id=user_id,
                previous_status=UserStatus.active.value,
                disable_minutes=30,
                restore_at=datetime.now(UTC) + timedelta(minutes=30),
                active=True,
            )
        )
        await other.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("meanwhile", "expected"),
    [(_admin_disabled, (UserStatus.disabled, 0, 0)), (_restricted_elsewhere, (UserStatus.disabled, 1, 1))],
    ids=["an-admin-disabled-the-user", "another-run-restricted-the-user"],
)
async def test_an_enforcement_retried_after_a_deadlock_rereads_and_acts_on_what_landed_in_between(
    recorder_db, pushed, meanwhile, expected
):
    user_id = await _active_user(recorder_db)

    async def land():
        await meanwhile(recorder_db, user_id)

    async with recorder_db() as db:
        state = _fail_commits(db, 1, _mysql_deadlock, land)
        await connection_limiter._enforce(db, [_observation(user_id)], SETTINGS)

    assert (state.failed, state.landed) == (1, 1)
    assert await _state(recorder_db, user_id) == expected, (
        "the retry replayed the restriction decided before the deadlock instead of re-reading the user"
    )
    assert pushed == []


@pytest.mark.asyncio
async def test_an_enforcement_that_keeps_hitting_lock_errors_is_postponed_with_a_warning_and_the_next_run_restricts(
    recorder_db, pushed, warnings
):
    user_id = await _active_user(recorder_db)

    async with recorder_db() as db:
        state = _fail_commits(db, connection_limiter.RETRIABLE_WRITE_ATTEMPTS, _mysql_deadlock)
        await connection_limiter._enforce(db, [_observation(user_id)], SETTINGS)

    assert state.failed == connection_limiter.RETRIABLE_WRITE_ATTEMPTS
    assert await _state(recorder_db, user_id) == (UserStatus.active, 0, 0)
    assert pushed == []
    assert len(warnings) == 1 and "next run retries it" in warnings[0], warnings

    async with recorder_db() as db:
        await connection_limiter._enforce(db, [_observation(user_id)], SETTINGS)

    assert await _state(recorder_db, user_id) == (UserStatus.disabled, 1, 1)
    assert pushed == [user_id]


@pytest.mark.asyncio
async def test_an_enforcement_that_fails_for_another_reason_raises_without_a_retry(recorder_db, pushed, warnings):
    user_id = await _active_user(recorder_db)

    async with recorder_db() as db:
        state = _fail_commits(db, 1, _unrelated)
        with pytest.raises(OperationalError):
            await connection_limiter._enforce(db, [_observation(user_id)], SETTINGS)

    assert state.commits == 1
    assert await _state(recorder_db, user_id) == (UserStatus.active, 0, 0)
    assert (pushed, warnings) == ([], [])


@pytest.mark.asyncio
async def test_forgetting_old_violations_that_keeps_deadlocking_is_postponed_and_keeps_the_restriction(
    monkeypatch: pytest.MonkeyPatch, recorder_db, pushed, warnings
):
    user_id = await _active_user(recorder_db)
    calls: list[int] = []

    async def deadlocks(_db, _days):
        calls.append(1)
        raise _mysql_deadlock()

    monkeypatch.setattr(connection_limiter, "forget_old", deadlocks)

    async with recorder_db() as db:
        await connection_limiter._enforce(db, [_observation(user_id)], SETTINGS)

    assert len(calls) == connection_limiter.RETRIABLE_WRITE_ATTEMPTS
    assert await _state(recorder_db, user_id) == (UserStatus.disabled, 1, 1)
    assert pushed == [user_id]
    assert len(warnings) == 1 and "forgetting old" in warnings[0], warnings
