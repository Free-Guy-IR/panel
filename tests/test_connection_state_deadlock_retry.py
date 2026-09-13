import asyncio

import pytest
from sqlalchemy.exc import OperationalError

from app.fork.jobs import connection_limiter as job


class _Deadlock(Exception):
    def __init__(self):
        super().__init__("Deadlock found when trying to get lock; try restarting transaction")
        self.args = (1213, "Deadlock found when trying to get lock; try restarting transaction")


def _deadlock_error():
    return OperationalError("INSERT INTO user_connection_states", {}, _Deadlock())


def _lock_wait_error():
    class _LockWait(Exception):
        def __init__(self):
            super().__init__("Lock wait timeout exceeded")
            self.args = (1205, "Lock wait timeout exceeded")

    return OperationalError("UPDATE users", {}, _LockWait())


def _postgres_deadlock():
    class _PgDeadlock(Exception):
        code = "40P01"

    return OperationalError("INSERT", {}, _PgDeadlock())


def _unrelated_error():
    class _Other(Exception):
        def __init__(self):
            super().__init__("Unknown column 'x' in field list")
            self.args = (1054, "Unknown column 'x' in field list")

    return OperationalError("SELECT", {}, _Other())


class _Session:
    def __init__(self):
        self.rollbacks = 0

    async def rollback(self):
        self.rollbacks += 1


@pytest.mark.parametrize("make_error", [_deadlock_error, _lock_wait_error, _postgres_deadlock])
def test_a_retriable_error_is_recognised(make_error):
    assert job._is_retriable_db_error(make_error())


def test_an_unrelated_database_error_is_not_retriable():
    assert not job._is_retriable_db_error(_unrelated_error())


def test_a_deadlock_is_retried_and_then_succeeds(monkeypatch):
    calls = {"n": 0}

    async def flaky(db, observations):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _deadlock_error()

    monkeypatch.setattr(job, "_write_states", flaky)
    monkeypatch.setattr(job, "RETRY_BACKOFF_SECONDS", 0)
    db = _Session()

    asyncio.run(job._persist_states(db, []))

    assert calls["n"] == 2
    assert db.rollbacks == 1


def test_retries_are_bounded_and_the_error_finally_surfaces(monkeypatch):
    calls = {"n": 0}

    async def always_deadlocks(db, observations):
        calls["n"] += 1
        raise _deadlock_error()

    monkeypatch.setattr(job, "_write_states", always_deadlocks)
    monkeypatch.setattr(job, "RETRY_BACKOFF_SECONDS", 0)
    db = _Session()

    with pytest.raises(OperationalError):
        asyncio.run(job._persist_states(db, []))

    assert calls["n"] == job.RETRIABLE_WRITE_ATTEMPTS
    assert db.rollbacks == job.RETRIABLE_WRITE_ATTEMPTS


def test_an_unrelated_error_is_not_retried(monkeypatch):
    calls = {"n": 0}

    async def fails(db, observations):
        calls["n"] += 1
        raise _unrelated_error()

    monkeypatch.setattr(job, "_write_states", fails)
    db = _Session()

    with pytest.raises(OperationalError):
        asyncio.run(job._persist_states(db, []))

    assert calls["n"] == 1
    assert db.rollbacks == 1
