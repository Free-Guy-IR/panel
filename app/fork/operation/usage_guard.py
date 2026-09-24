from collections.abc import AsyncIterator, Hashable
from contextlib import asynccontextmanager

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

USAGE_STATEMENT_TIMEOUT_MS = 60_000
MYSQL_STATEMENT_TIMEOUT_ERROR = 3024
POSTGRES_QUERY_CANCELED = "57014"

_in_flight: set[Hashable] = set()


@asynccontextmanager
async def usage_aggregation_guard(key: Hashable) -> AsyncIterator[None]:
    if key in _in_flight:
        raise HTTPException(status_code=429, detail="A usage query for this account is already running")
    _in_flight.add(key)
    try:
        yield
    finally:
        _in_flight.discard(key)


def _is_statement_timeout(exc: DBAPIError) -> bool:
    orig = getattr(exc, "orig", None)
    args = getattr(orig, "args", ())
    if args and args[0] == MYSQL_STATEMENT_TIMEOUT_ERROR:
        return True
    return getattr(orig, "sqlstate", None) == POSTGRES_QUERY_CANCELED or getattr(orig, "pgcode", None) == (
        POSTGRES_QUERY_CANCELED
    )


async def execute_with_statement_timeout(db: AsyncSession, stmt: Select):
    dialect = db.bind.dialect.name
    if dialect in ("mysql", "mariadb"):
        stmt = stmt.prefix_with(f"/*+ MAX_EXECUTION_TIME({USAGE_STATEMENT_TIMEOUT_MS}) */", dialect=dialect)
    elif dialect == "postgresql":
        await db.execute(text(f"SET LOCAL statement_timeout = {USAGE_STATEMENT_TIMEOUT_MS}"))
    try:
        return await db.execute(stmt)
    except DBAPIError as exc:
        if not _is_statement_timeout(exc):
            raise
        await db.rollback()
        raise HTTPException(
            status_code=503, detail="The usage query took too long; narrow the date range and try again"
        ) from exc
