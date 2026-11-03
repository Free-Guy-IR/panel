import asyncio
from collections.abc import Sequence
from typing import Any

import httpx
from sqlalchemy import event
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import base
from tests.api import DATABASE_URL, IS_SQLITE, app

Call = tuple[str, str, dict[str, Any]]


def _own_connection_engine():
    connect_args = {"timeout": 30} if IS_SQLITE else {}
    engine = create_async_engine(DATABASE_URL, poolclass=NullPool, connect_args=connect_args)
    if IS_SQLITE:

        @event.listens_for(engine.sync_engine, "connect")
        def _enforce_sqlite_foreign_keys(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


async def _send(calls: Sequence[Call]) -> list[httpx.Response]:
    engine = _own_connection_engine()
    sessions = async_sessionmaker(autocommit=False, autoflush=False, expire_on_commit=False, bind=engine)

    async def own_connection_db():
        db = sessions()
        try:
            yield db
        except SQLAlchemyError:
            await db.rollback()
            raise
        finally:
            await db.close()

    previous = app.dependency_overrides[base.get_db]
    app.dependency_overrides[base.get_db] = own_connection_db
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            return await asyncio.gather(*(http.request(method, url, **kwargs) for method, url, kwargs in calls))
    finally:
        app.dependency_overrides[base.get_db] = previous
        await engine.dispose()


def send_concurrently(calls: Sequence[Call]) -> list[httpx.Response]:
    return asyncio.run(_send(calls))
