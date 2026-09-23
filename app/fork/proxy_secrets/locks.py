from __future__ import annotations

from datetime import UTC, datetime
from weakref import WeakKeyDictionary

from sqlalchemy import Engine, insert, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import AsyncSession
from app.fork.models.named_lock import NamedLock
from config import database_settings

PROXY_SECRETS_LOCK = "proxy_secrets"
MYSQL_FAMILY = frozenset({"mysql", "mariadb"})
HELD_LOCKS_KEY = "fork_named_locks"
MYSQL_LOCK_WAIT_TIMEOUT = 1205
POSTGRES_LOCK_NOT_AVAILABLE = "55P03"
SQLITE_LOCKED = "database is locked"
LOCK_WAIT_TIMEOUT_DETAIL = (
    "another request is writing proxy secrets and this one timed out waiting for it; retry shortly"
)


class NamedLockMissing(RuntimeError):
    pass


_committed_readers: WeakKeyDictionary[Engine, AsyncEngine] = WeakKeyDictionary()


def named_lock_seed_statement(dialect_name: str, name: str):
    table = NamedLock.__table__
    values = {"name": name, "acquired_at": None}
    if dialect_name == "postgresql":
        return pg_insert(table).values(values).on_conflict_do_nothing(index_elements=["name"])
    if dialect_name in MYSQL_FAMILY:
        return mysql_insert(table).values(values).on_duplicate_key_update(name=table.c.name)
    return insert(table).values(values).prefix_with("OR IGNORE")


async def acquire_named_lock(db: AsyncSession, name: str) -> None:
    held = db.info.setdefault(HELD_LOCKS_KEY, {})
    transaction = db.sync_session.get_transaction()
    if transaction is not None and held.get(name) is transaction:
        return

    table = NamedLock.__table__
    take = update(table).where(table.c.name == name).values(acquired_at=datetime.now(UTC))
    with db.no_autoflush:
        result = await db.execute(take)
        if result.rowcount == 0:
            await db.execute(named_lock_seed_statement(db.get_bind().dialect.name, name))
            result = await db.execute(take)
    if result.rowcount != 1:
        raise NamedLockMissing(f"the {name!r} named lock row could not be taken")
    held[name] = db.sync_session.get_transaction()


async def acquire_proxy_secrets_lock(db: AsyncSession) -> None:
    await acquire_named_lock(db, PROXY_SECRETS_LOCK)


def committed_reader(engine: Engine) -> AsyncEngine:
    reader = _committed_readers.get(engine)
    if reader is None:
        reader = create_async_engine(
            engine.url,
            isolation_level="READ COMMITTED",
            poolclass=NullPool,
            connect_args={"connect_timeout": database_settings.connect_timeout},
        )
        _committed_readers[engine] = reader
    return reader


async def read_committed_rows(db: AsyncSession, statement) -> list:
    bind = db.get_bind()
    if bind.dialect.name not in MYSQL_FAMILY:
        return list((await db.execute(statement)).all())
    async with committed_reader(bind.engine).connect() as fresh:
        return list((await fresh.execute(statement)).all())


def is_lock_wait_timeout(exc: BaseException) -> bool:
    if not isinstance(exc, DBAPIError):
        return False
    original = exc.orig
    arguments = getattr(original, "args", ()) or ()
    if arguments and arguments[0] == MYSQL_LOCK_WAIT_TIMEOUT:
        return True
    if POSTGRES_LOCK_NOT_AVAILABLE in (getattr(original, "sqlstate", None), getattr(original, "pgcode", None)):
        return True
    return SQLITE_LOCKED in str(original)
