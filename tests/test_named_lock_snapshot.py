import asyncio
import os

import pytest
import pytest_asyncio
from sqlalchemy import delete, event, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.models import Admin, AdminRole, Base, User
from app.fork.models.named_lock import NamedLock
from app.fork.proxy_secrets.locks import PROXY_SECRETS_LOCK, acquire_named_lock, named_lock_seed_statement
from app.models.proxy import ProxyTable
from config import database_settings

GITHUB_MODE = os.getenv("TEST_FROM", "local").lower() != "local"
PREFIX = "snap-"
HOLD_SECONDS = 0.5
TABLE = NamedLock.__table__


def _foreign_keys_on(dbapi_connection, _record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


async def _purge(engine):
    async with engine.begin() as conn:
        await conn.execute(delete(User.__table__).where(User.username.like(f"{PREFIX}%")))
        await conn.execute(delete(Admin.__table__).where(Admin.username.like(f"{PREFIX}%")))
        await conn.execute(delete(AdminRole.__table__).where(AdminRole.name.like(f"{PREFIX}%")))


async def _seed_lock_row(engine):
    async with engine.begin() as conn:
        await conn.execute(named_lock_seed_statement(engine.dialect.name, PROXY_SECRETS_LOCK))


@pytest_asyncio.fixture
async def engine(tmp_path):
    if GITHUB_MODE:
        engine = create_async_engine(database_settings.url, poolclass=NullPool)
        await _purge(engine)
        await _seed_lock_row(engine)
        yield engine
        await _purge(engine)
        await _seed_lock_row(engine)
        await engine.dispose()
        return
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'snapshot.db'}")
    event.listen(engine.sync_engine, "connect", _foreign_keys_on)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def sessions(engine):
    return async_sessionmaker(engine, class_=AsyncSession, autoflush=False, expire_on_commit=False)


def _named_lock_statements(engine):
    statements = []

    def record(conn, cursor, statement, *rest):
        if "named_locks" in statement:
            statements.append(statement.lstrip().split()[0].upper())

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    return statements


async def _user_with_note(sessions, note):
    async with sessions() as db:
        role = AdminRole(name=f"{PREFIX}owner", is_owner=True)
        db.add(role)
        await db.flush()
        admin = Admin(username=f"{PREFIX}owner", hashed_password="x", role_id=role.id)
        db.add(admin)
        await db.flush()
        user = User(username=f"{PREFIX}user", proxy_settings=ProxyTable().dict(), admin_id=admin.id)
        user.note = note
        db.add(user)
        await db.commit()
        return user.id


@pytest.mark.parametrize("row", ["present", "missing"])
@pytest.mark.asyncio
async def test_the_lock_row_is_written_before_anything_reads_it(engine, sessions, row):
    if row == "missing":
        async with sessions() as db:
            await db.execute(delete(TABLE).where(TABLE.c.name == PROXY_SECRETS_LOCK))
            await db.commit()
    statements = _named_lock_statements(engine)

    async with sessions() as db:
        await acquire_named_lock(db, PROXY_SECRETS_LOCK)
        await db.commit()

    assert statements, "the lock was taken without touching named_locks"
    assert statements[0] == "UPDATE", f"named_locks was read before the blocking write: {statements}"


@pytest.mark.asyncio
async def test_a_lock_taken_inside_a_rolled_back_savepoint_is_taken_again(engine, sessions):
    await _seed_lock_row(engine)
    statements = _named_lock_statements(engine)

    async with sessions() as db:
        await db.execute(select(1))
        savepoint = await db.begin_nested()
        await acquire_named_lock(db, PROXY_SECRETS_LOCK)
        await savepoint.rollback()
        taken_in_savepoint = statements.count("UPDATE")
        await acquire_named_lock(db, PROXY_SECRETS_LOCK)
        taken_again = statements.count("UPDATE")
        await acquire_named_lock(db, PROXY_SECRETS_LOCK)
        taken_a_third_time = statements.count("UPDATE")
        await db.rollback()

    assert taken_in_savepoint == 1
    assert taken_again == 2
    assert taken_a_third_time == taken_again


@pytest.mark.asyncio
async def test_a_read_under_the_lock_sees_what_the_previous_holder_committed(engine, sessions):
    if engine.dialect.name not in ("mysql", "mariadb"):
        pytest.skip(reason="repeatable-read snapshots opened before a lock wait exist only on MySQL and MariaDB")
    user_id = await _user_with_note(sessions, "before")
    waiting = asyncio.Event()

    async def waiter():
        async with sessions() as db:
            waiting.set()
            await acquire_named_lock(db, PROXY_SECRETS_LOCK)
            note = await db.scalar(select(User.note).where(User.id == user_id))
            await db.rollback()
            return note

    async with sessions() as holder:
        await acquire_named_lock(holder, PROXY_SECRETS_LOCK)
        await holder.execute(update(User).where(User.id == user_id).values(note="holder"))
        task = asyncio.create_task(waiter())
        await waiting.wait()
        await asyncio.sleep(HOLD_SECONDS)
        assert not task.done()
        await holder.commit()

    assert await task == "holder"


@pytest.mark.asyncio
async def test_a_lock_released_by_a_savepoint_rollback_is_not_left_unheld(engine, sessions):
    if engine.dialect.name != "postgresql":
        pytest.skip(reason="only PostgreSQL releases a row lock when the savepoint that took it is rolled back")

    async with sessions() as holder:
        await holder.execute(select(1))
        savepoint = await holder.begin_nested()
        await acquire_named_lock(holder, PROXY_SECRETS_LOCK)
        await savepoint.rollback()
        await acquire_named_lock(holder, PROXY_SECRETS_LOCK)

        async with sessions() as rival:
            await rival.execute(text("SET lock_timeout = '1s'"))
            with pytest.raises(DBAPIError) as refused:
                await acquire_named_lock(rival, PROXY_SECRETS_LOCK)
            await rival.rollback()
        await holder.rollback()

    assert "55P03" in {getattr(refused.value.orig, "sqlstate", None), getattr(refused.value.orig, "pgcode", None)}
