from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import mysql, postgresql, sqlite

from app.db.crud import bulk as bulk_crud
from app.db.models import User, UserStatus
from app.fork.crud import user_locks

MYSQL_FAMILY_DIALECTS = (mysql.dialect(), mysql.dialect(is_mariadb=True))
OTHER_DIALECTS = (postgresql.dialect(), sqlite.dialect())
HINTED_FROM = "FROM users FORCE INDEX (PRIMARY)"


class _NoRows:
    rowcount = 0

    def all(self):
        return []

    def first(self):
        return None

    def scalars(self):
        return self


class _RecordingSession:
    def __init__(self):
        self.statements = []

    def get_bind(self):
        return SimpleNamespace(dialect=mysql.dialect())

    async def execute(self, statement):
        self.statements.append(statement)
        return _NoRows()

    async def commit(self):
        return None

    async def rollback(self):
        return None


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
async def test_lock_user_rows_locks_users_through_the_primary_key_on_the_mysql_family_only():
    session = _RecordingSession()

    await user_locks.lock_user_rows(session, [3, 1, 2])

    _assert_only_the_mysql_family_forces_the_primary_key(_locking_selects(session.statements))


@pytest.mark.asyncio
async def test_a_bulk_status_change_rereads_its_locked_users_through_the_primary_key_on_the_mysql_family_only():
    session = _RecordingSession()
    locked = {1: UserStatus.active, 2: UserStatus.active, 3: UserStatus.expired}

    await bulk_crud._apply_to_locked_targets(session, locked, (), "data_limit", 1, data_limit=1)

    _assert_only_the_mysql_family_forces_the_primary_key(_locking_selects(session.statements))
    updates = [statement for statement in session.statements if statement.is_update]
    assert updates
    for statement in updates:
        for dialect in MYSQL_FAMILY_DIALECTS:
            assert "UPDATE users FORCE INDEX (PRIMARY)" in str(statement.compile(dialect=dialect)), dialect.name
        for dialect in OTHER_DIALECTS:
            assert "FORCE INDEX" not in str(statement.compile(dialect=dialect)), dialect.name


@pytest.mark.asyncio
async def test_a_bulk_status_change_locks_and_rechecks_its_targets_through_the_primary_key_on_the_mysql_family_only():
    session = _RecordingSession()

    with pytest.raises(bulk_crud.BulkUserUpdateConflict):
        await bulk_crud._lock_bulk_targets(session, User.admin_id == 1, [3, 1, 2])

    _assert_only_the_mysql_family_forces_the_primary_key(_locking_selects(session.statements))


@pytest.mark.asyncio
async def test_a_bulk_status_change_reads_its_unlisted_targets_without_locking():
    session = _RecordingSession()
    session.get_bind = lambda: SimpleNamespace(dialect=sqlite.dialect())

    await bulk_crud._lock_bulk_targets(session, User.admin_id == 1)

    assert session.statements
    assert getattr(session.statements[0], "_for_update_arg", None) is None


@pytest.mark.asyncio
async def test_the_out_of_range_check_locks_through_the_primary_key_on_the_mysql_family_only():
    session = _RecordingSession()

    locked = {2: UserStatus.active, 1: UserStatus.active}

    await bulk_crud._reject_out_of_range_locked_targets(session, locked, "data_limit", 1)

    _assert_only_the_mysql_family_forces_the_primary_key(_locking_selects(session.statements))


@pytest.mark.asyncio
async def test_reactivating_limited_users_rereads_them_through_the_primary_key_on_the_mysql_family_only():
    session = _RecordingSession()

    await user_locks.reactivate_limited_users(session, [SimpleNamespace(id=2), SimpleNamespace(id=1)])

    _assert_only_the_mysql_family_forces_the_primary_key(_locking_selects(session.statements))
