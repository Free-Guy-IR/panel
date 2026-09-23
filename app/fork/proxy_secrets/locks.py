from __future__ import annotations

from datetime import UTC, datetime
from weakref import WeakKeyDictionary

from sqlalchemy import Engine, insert, select, update
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
SEEN_LOCK_ROWS_KEY = "fork_named_lock_rows_seen"
MYSQL_LOCK_WAIT_TIMEOUT = 1205
MYSQL_DEADLOCK = 1213
POSTGRES_LOCK_NOT_AVAILABLE = "55P03"
POSTGRES_DEADLOCK = "40P01"
SQLITE_LOCKED = "database is locked"
SEED_ATTEMPTS = 2
LOCK_UNAVAILABLE_DETAIL = (
    "another request is writing proxy secrets and this one could not take the lock in time; "
    "nothing was stored, retry shortly"
)
GROUP_LOCK_UNAVAILABLE_DETAIL = (
    "the group change was saved, but giving its users their WireGuard addresses and entitled secrets could not "
    "take the proxy secrets lock in time; that step was not stored and is being completed automatically in the "
    "background"
)


class NamedLockMissing(RuntimeError):
    pass


_side_engines: WeakKeyDictionary[Engine, AsyncEngine] = WeakKeyDictionary()


def named_lock_seed_statement(dialect_name: str, name: str):
    table = NamedLock.__table__
    values = {"name": name, "acquired_at": None}
    if dialect_name == "postgresql":
        return pg_insert(table).values(values).on_conflict_do_nothing(index_elements=["name"])
    if dialect_name in MYSQL_FAMILY:
        return mysql_insert(table).values(values).on_duplicate_key_update(name=table.c.name)
    return insert(table).values(values).prefix_with("OR IGNORE")


async def seed_named_lock_outside(engine, dialect_name: str, name: str) -> None:
    for attempt in range(1, SEED_ATTEMPTS + 1):
        try:
            async with engine.begin() as connection:
                await connection.execute(named_lock_seed_statement(dialect_name, name))
            return
        except DBAPIError as exc:
            if attempt == SEED_ATTEMPTS or not is_deadlock(exc):
                raise


async def _named_lock_row_exists_outside(engine, name: str) -> bool:
    table = NamedLock.__table__
    async with engine.connect() as connection:
        found = await connection.scalar(select(table.c.name).where(table.c.name == name))
    return found is not None


async def _seed_named_lock(db: AsyncSession, name: str) -> None:
    bind = db.get_bind()
    dialect_name = bind.dialect.name
    if dialect_name == "sqlite":
        await db.execute(named_lock_seed_statement(dialect_name, name))
        return
    side = side_engine(bind.engine)
    if await _named_lock_row_exists_outside(side, name):
        return
    await seed_named_lock_outside(side, dialect_name, name)


async def _seed_before_blocking(db: AsyncSession, name: str) -> None:
    seen = db.info.setdefault(SEEN_LOCK_ROWS_KEY, set())
    if name in seen:
        return
    await _seed_named_lock(db, name)
    seen.add(name)


def _innermost_transaction(db: AsyncSession):
    session = db.sync_session
    return session.get_nested_transaction() or session.get_transaction()


async def acquire_named_lock(db: AsyncSession, name: str) -> None:
    held = db.info.setdefault(HELD_LOCKS_KEY, {})
    transaction = _innermost_transaction(db)
    if transaction is not None and held.get(name) is transaction:
        return

    table = NamedLock.__table__
    take = update(table).where(table.c.name == name).values(acquired_at=datetime.now(UTC))
    gap_locking = db.get_bind().dialect.name in MYSQL_FAMILY
    with db.no_autoflush:
        if gap_locking:
            await _seed_before_blocking(db, name)
        result = await db.execute(take)
        if result.rowcount == 0 and not gap_locking:
            await _seed_named_lock(db, name)
            result = await db.execute(take)
    if result.rowcount != 1:
        db.info.get(SEEN_LOCK_ROWS_KEY, set()).discard(name)
        raise NamedLockMissing(f"the {name!r} named lock row could not be taken")
    held[name] = _innermost_transaction(db)


async def acquire_proxy_secrets_lock(db: AsyncSession) -> None:
    await acquire_named_lock(db, PROXY_SECRETS_LOCK)


def side_connect_args(url) -> dict:
    backend = url.get_backend_name()
    if backend in MYSQL_FAMILY:
        return {"connect_timeout": database_settings.connect_timeout}
    if backend == "postgresql":
        if url.get_driver_name() == "asyncpg":
            return {"timeout": database_settings.connect_timeout}
        return {"connect_timeout": database_settings.connect_timeout}
    return {}


def side_engine(engine: Engine) -> AsyncEngine:
    side = _side_engines.get(engine)
    if side is None:
        side = create_async_engine(
            engine.url,
            isolation_level="READ COMMITTED",
            poolclass=NullPool,
            hide_parameters=True,
            connect_args=side_connect_args(engine.url),
        )
        _side_engines[engine] = side
    return side


async def read_committed_rows(db: AsyncSession, statement) -> list:
    bind = db.get_bind()
    if bind.dialect.name not in MYSQL_FAMILY:
        return list((await db.execute(statement)).all())
    async with side_engine(bind.engine).connect() as fresh:
        return list((await fresh.execute(statement)).all())


def driver_code(exc: DBAPIError):
    arguments = getattr(exc.orig, "args", ()) or ()
    return arguments[0] if arguments else None


def _driver_sqlstate(exc: DBAPIError) -> set:
    return {getattr(exc.orig, "sqlstate", None), getattr(exc.orig, "pgcode", None)}


def is_deadlock(exc: BaseException) -> bool:
    if not isinstance(exc, DBAPIError):
        return False
    return driver_code(exc) == MYSQL_DEADLOCK or POSTGRES_DEADLOCK in _driver_sqlstate(exc)


def is_lock_unavailable(exc: BaseException) -> bool:
    return isinstance(exc, NamedLockMissing) or is_lock_wait_timeout(exc) or is_deadlock(exc)


def is_lock_wait_timeout(exc: BaseException) -> bool:
    if not isinstance(exc, DBAPIError):
        return False
    if driver_code(exc) == MYSQL_LOCK_WAIT_TIMEOUT or POSTGRES_LOCK_NOT_AVAILABLE in _driver_sqlstate(exc):
        return True
    return SQLITE_LOCKED in str(exc.orig)
