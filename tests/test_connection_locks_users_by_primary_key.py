from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import mysql, postgresql, sqlite

from app.db.crud import bulk as bulk_crud
from app.db.models import ConnectionRestriction, User, UserStatus
from app.fork.crud import user_locks
from app.fork.jobs import connection_limiter as connection_limiter_job
from app.utils import connection_enforcement

MYSQL_FAMILY_DIALECTS = (mysql.dialect(), mysql.dialect(is_mariadb=True))
OTHER_DIALECTS = (postgresql.dialect(), sqlite.dialect())
HINTED_FROM = "FROM users FORCE INDEX (PRIMARY)"


class _NoRows:
    def all(self):
        return []

    def scalars(self):
        return self


class _RecordingSession:
    def __init__(self):
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _NoRows()


def _locking_selects(statements) -> list:
    return [statement for statement in statements if getattr(statement, "_for_update_arg", None) is not None]


def _assert_only_the_mysql_family_forces_the_primary_key(statements) -> None:
    assert statements
    for statement in statements:
        for dialect in MYSQL_FAMILY_DIALECTS:
            compiled = str(statement.compile(dialect=dialect))
            assert HINTED_FROM in compiled, (
                "a FOR UPDATE by id answered from idx_users_admin_status deadlocks with an owner move",
                dialect.name,
                compiled,
            )
        for dialect in OTHER_DIALECTS:
            assert "FORCE INDEX" not in str(statement.compile(dialect=dialect)), dialect.name


@pytest.mark.asyncio
async def test_connection_enforcement_locks_users_through_the_primary_key_on_the_mysql_family_only():
    session = _RecordingSession()

    await connection_enforcement._lock_user_rows(session, [3, 1, 2])

    _assert_only_the_mysql_family_forces_the_primary_key(_locking_selects(session.statements))


@pytest.mark.asyncio
async def test_connection_enforcement_locks_restrictions_without_a_users_index_hint():
    session = _RecordingSession()

    await connection_enforcement._lock_rows_by_id(session, ConnectionRestriction, [3, 1, 2])

    statements = _locking_selects(session.statements)
    assert statements
    for statement in statements:
        for dialect in MYSQL_FAMILY_DIALECTS + OTHER_DIALECTS:
            assert "FORCE INDEX" not in str(statement.compile(dialect=dialect)), dialect.name


@pytest.mark.asyncio
async def test_connection_limiter_locks_still_active_users_through_the_primary_key_on_the_mysql_family_only():
    session = _RecordingSession()

    await connection_limiter_job._lock_still_active_users(session, [3, 1, 2])

    _assert_only_the_mysql_family_forces_the_primary_key(_locking_selects(session.statements))


def _in_list_values(statement) -> list:
    params = statement.compile(dialect=mysql.dialect()).construct_params()
    lists = [value for value in params.values() if isinstance(value, list | tuple)]
    assert len(lists) == 1, params
    return list(lists[0])


@pytest.mark.asyncio
async def test_connection_limiter_locks_still_active_users_in_ascending_chunks_of_the_user_lock_batch_size():
    session = _RecordingSession()
    user_ids = list(range(2 * user_locks.USER_LOCK_BATCH_SIZE + 7, 0, -1))

    await connection_limiter_job._lock_still_active_users(session, user_ids)

    chunks = [_in_list_values(statement) for statement in _locking_selects(session.statements)]
    assert all(len(chunk) <= user_locks.USER_LOCK_BATCH_SIZE for chunk in chunks), [len(chunk) for chunk in chunks]
    assert [user_id for chunk in chunks for user_id in chunk] == sorted(user_ids)


class _FullRowcount:
    def __init__(self, rowcount: int):
        self.rowcount = rowcount

    def all(self):
        return []

    def first(self):
        return None


class _CommittingRecordingSession(_RecordingSession):
    def get_bind(self):
        return SimpleNamespace(dialect=mysql.dialect())

    async def execute(self, statement):
        self.statements.append(statement)
        if statement.is_update:
            return _FullRowcount(len(_in_list_values(statement)))
        return _FullRowcount(0)

    async def commit(self):
        return None

    async def rollback(self):
        return None


@pytest.mark.asyncio
async def test_bulk_status_targets_are_locked_within_the_user_lock_batch_size_in_ascending_order():
    session = _CommittingRecordingSession()
    user_ids = list(range(2 * user_locks.USER_LOCK_BATCH_SIZE + 7, 0, -1))

    with pytest.raises(bulk_crud.BulkUserUpdateConflict):
        await bulk_crud._lock_bulk_targets(session, User.admin_id == 1, user_ids)

    chunks = [_in_list_values(statement) for statement in _locking_selects(session.statements)]
    assert all(len(chunk) <= user_locks.USER_LOCK_BATCH_SIZE for chunk in chunks), [len(c) for c in chunks]
    assert [user_id for chunk in chunks for user_id in chunk] == sorted(user_ids)


@pytest.mark.asyncio
async def test_bulk_status_statements_stay_within_the_user_lock_batch_size_in_ascending_order():
    session = _CommittingRecordingSession()
    user_ids = list(range(2 * user_locks.USER_LOCK_BATCH_SIZE + 7, 0, -1))
    locked = dict.fromkeys(user_ids, UserStatus.active)

    await bulk_crud._apply_to_locked_targets(session, locked, (), "data_limit", 1, data_limit=1)

    updates = [statement for statement in session.statements if statement.is_update]
    locking = _locking_selects(session.statements)
    for statements in (updates, locking):
        chunks = [_in_list_values(statement) for statement in statements]
        assert all(len(chunk) <= user_locks.USER_LOCK_BATCH_SIZE for chunk in chunks), [len(c) for c in chunks]
        assert [user_id for chunk in chunks for user_id in chunk] == sorted(user_ids)
