from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import update
from sqlalchemy.dialects import mysql, postgresql, sqlite

from app.db.crud import bulk as bulk_crud
from app.db.models import User
from app.fork.crud import user_locks
from app.fork.crud.user_locks import USER_LOCK_BATCH_SIZE
from app.fork.jobs import connection_limiter
from app.models.user import BulkUser
from tests.test_recorder_harness import recorder_db_fixture, seed_users_and_nodes  # noqa: F401

MYSQL_FAMILY_DIALECTS = (mysql.dialect(), mysql.dialect(is_mariadb=True))
OTHER_DIALECTS = (postgresql.dialect(), sqlite.dialect())
CANDIDATES = USER_LOCK_BATCH_SIZE * 2 + 7


class _Rows:
    rowcount = 0

    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def unique(self):
        return self

    def scalars(self):
        return self


class _CandidateSession:
    def __init__(self, dialect=None):
        self.statements = []
        self._dialect = dialect or sqlite.dialect()

    def get_bind(self):
        return SimpleNamespace(dialect=self._dialect)

    async def execute(self, statement):
        self.statements.append(statement)
        if not statement.is_select:
            return _Rows([])
        if getattr(statement, "_for_update_arg", None) is None:
            return _Rows([(user_id,) for user_id in range(CANDIDATES, 0, -1)])
        return _Rows([(user_id, None) for user_id in _in_list(statement)])


class _EmptySession(_CandidateSession):
    async def execute(self, statement):
        self.statements.append(statement)
        return _Rows([])


def _locking(statements) -> list:
    return [statement for statement in statements if getattr(statement, "_for_update_arg", None) is not None]


def _in_list(statement) -> list[int]:
    lists = [value for value in statement.compile().params.values() if isinstance(value, list | tuple)]
    assert len(lists) == 1, statement
    return list(lists[0])


def _assert_hinted_on_the_mysql_family_only(statement, prefix: str) -> None:
    for dialect in MYSQL_FAMILY_DIALECTS:
        assert f"{prefix} users FORCE INDEX (PRIMARY)" in str(statement.compile(dialect=dialect)), dialect.name
    for dialect in OTHER_DIALECTS:
        assert "FORCE INDEX" not in str(statement.compile(dialect=dialect)), dialect.name


def _bulk(amount: int = 3600) -> BulkUser:
    return BulkUser(amount=amount)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["expire", "data_limit"])
async def test_limit_targets_read_without_a_lock_then_lock_by_primary_key_in_ascending_bounded_chunks(field):
    session = _CandidateSession()

    await bulk_crud.get_bulk_user_limit_targets(session, _bulk(), field, None, lock=True)

    assert getattr(session.statements[0], "_for_update_arg", None) is None
    locking = _locking(session.statements)
    assert len(locking) == -(-CANDIDATES // USER_LOCK_BATCH_SIZE)
    seen: list[int] = []
    for statement in locking:
        _assert_hinted_on_the_mysql_family_only(statement, "FROM")
        chunk = _in_list(statement)
        assert len(chunk) <= USER_LOCK_BATCH_SIZE
        assert chunk == sorted(chunk)
        seen.extend(chunk)
    assert seen == list(range(1, CANDIDATES + 1))


@pytest.mark.asyncio
async def test_limit_targets_without_a_lock_take_no_row_lock():
    session = _CandidateSession()

    await bulk_crud.get_bulk_user_limit_targets(session, _bulk(), "data_limit", None, lock=False)

    assert session.statements
    assert not _locking(session.statements)


@pytest.mark.asyncio
async def test_reactivating_limited_users_updates_them_through_the_primary_key_on_the_mysql_family_only():
    session = _EmptySession()

    await user_locks.reactivate_limited_users(session, [SimpleNamespace(id=2), SimpleNamespace(id=1)])

    updates = [statement for statement in session.statements if statement.is_update]
    assert updates
    for statement in updates:
        _assert_hinted_on_the_mysql_family_only(statement, "UPDATE")


@pytest.mark.asyncio
async def test_the_limiter_reloads_its_locked_users_in_bounded_ascending_chunks():
    session = _EmptySession()

    await connection_limiter._reload_locked_users(session, range(CANDIDATES, 0, -1))

    assert len(session.statements) == -(-CANDIDATES // USER_LOCK_BATCH_SIZE)
    seen: list[int] = []
    for statement in session.statements:
        chunk = _in_list(statement)
        assert len(chunk) <= USER_LOCK_BATCH_SIZE
        seen.extend(chunk)
    assert seen == list(range(1, CANDIDATES + 1))


async def _limit_targets(session, field):
    return [user_id for user_id, _ in await bulk_crud.get_bulk_user_limit_targets(session, _bulk(), field, None)]


async def _status_targets(session, field):
    return sorted(await bulk_crud._lock_bulk_targets(session, bulk_crud._bulk_user_target_filter(_bulk(), field, None)))


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["expire", "data_limit"])
@pytest.mark.parametrize("lock_targets", [_limit_targets, _status_targets])
async def test_a_user_that_starts_matching_after_the_unlocked_read_is_still_locked(
    recorder_db, monkeypatch: pytest.MonkeyPatch, field, lock_targets
):
    _, (matching, late), _ = await seed_users_and_nodes(recorder_db, user_count=2, node_count=0)
    future = datetime.now(UTC) + timedelta(days=1)
    async with recorder_db() as session:
        await session.execute(update(User).where(User.id == matching).values(expire=future, data_limit=10**9))
        await session.execute(update(User).where(User.id == late).values(expire=None, data_limit=None))
        await session.commit()

    real = bulk_crud.read_committed_rows
    calls = []

    async def writer_commits_in_the_window(db, statement):
        rows = await real(db, statement)
        calls.append(rows)
        if len(calls) == 1:
            async with recorder_db() as writer:
                await writer.execute(update(User).where(User.id == late).values(expire=future, data_limit=10**9))
                await writer.commit()
        return rows

    monkeypatch.setattr(bulk_crud, "read_committed_rows", writer_commits_in_the_window)

    async with recorder_db() as session:
        targets = await lock_targets(session, field)
        await session.rollback()

    assert late not in {user_id for (user_id,) in calls[0]}
    assert targets == sorted([matching, late])
