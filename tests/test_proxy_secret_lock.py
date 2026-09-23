import asyncio
import os
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import delete, event, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.models import Admin, AdminRole, Base, User
from app.fork.proxy_secrets import SPEC_BY_FIELD, ProxySecretField, enforce_unique_proxy_secrets, secret_column
from app.models.proxy import ProxyTable
from app.models.user import UserCreate
from app.operation import OperatorType
from app.operation.user import UserOperation
from config import database_settings

GITHUB_MODE = os.getenv("TEST_FROM", "local").lower() != "local"
PREFIX = "lock-"
USERNAME = "locker"
TROJAN = "a-trojan-password-two-users-want"
VLESS_ID = "22222222-2222-4222-8222-222222222222"
MYSQL_FAMILY = ("mysql", "mariadb")
WAIT_SECONDS = 1.0
CHECK_TO_WRITE_SECONDS = 0.05


def _foreign_keys_on(dbapi_connection, _record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


async def _purge(engine):
    async with engine.begin() as conn:
        await conn.execute(delete(User.__table__).where(User.username.like(f"{USERNAME}%")))
        await conn.execute(delete(Admin.__table__).where(Admin.username.like(f"{PREFIX}%")))
        await conn.execute(delete(AdminRole.__table__).where(AdminRole.name.like(f"{PREFIX}%")))


@pytest_asyncio.fixture
async def engine(tmp_path):
    if GITHUB_MODE:
        engine = create_async_engine(database_settings.url, poolclass=NullPool)
        await _purge(engine)
        yield engine
        await _purge(engine)
        await engine.dispose()
        return
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'lock.db'}")
    event.listen(engine.sync_engine, "connect", _foreign_keys_on)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def sessions(engine):
    return async_sessionmaker(engine, class_=AsyncSession, autoflush=False, expire_on_commit=False)


@pytest_asyncio.fixture
async def admin_id(sessions):
    async with sessions() as db:
        role = AdminRole(name=f"{PREFIX}owner", is_owner=True)
        db.add(role)
        await db.flush()
        admin = Admin(username=f"{PREFIX}owner", hashed_password="x", role_id=role.id)
        db.add(admin)
        await db.commit()
        return admin.id


def _operation():
    operation = UserOperation.__new__(UserOperation)
    operation.operator_type = OperatorType.API
    return operation


def _trojan(password):
    return ProxyTable(trojan={"password": password})


async def _holders(sessions, field):
    column = secret_column(SPEC_BY_FIELD[field])
    async with sessions() as db:
        stmt = select(column, func.count(User.id)).where(User.username.like(f"{USERNAME}%")).group_by(column)
        return {value: count for value, count in (await db.execute(stmt)).all() if value}


async def _add_user(db, admin_id, username, proxy_settings):
    db.add(User(username=username, proxy_settings=proxy_settings.dict(), admin_id=admin_id))
    await db.flush()


@pytest.mark.asyncio
async def test_a_value_committed_after_the_readers_snapshot_is_seen_on_mysql(engine, sessions, admin_id):
    if engine.dialect.name not in MYSQL_FAMILY:
        pytest.skip(reason="repeatable-read request snapshots exist only on MySQL and MariaDB")

    async with sessions() as reader:
        await reader.execute(select(User.id).limit(1))

        async with sessions() as writer:
            await _add_user(writer, admin_id, f"{USERNAME}-writer", _trojan(TROJAN))
            await writer.commit()

        prepared = await enforce_unique_proxy_secrets(reader, _trojan(TROJAN))
        await _add_user(reader, admin_id, f"{USERNAME}-reader", prepared)
        await reader.commit()

    assert prepared.trojan.password != TROJAN
    assert (await _holders(sessions, ProxySecretField.trojan_password))[TROJAN] == 1


@pytest.mark.asyncio
async def test_a_second_writer_waits_for_the_first_and_then_sees_its_value(sessions, admin_id):
    connected = asyncio.Event()

    async def second_writer():
        async with sessions() as db:
            await db.execute(select(1))
            connected.set()
            prepared = await enforce_unique_proxy_secrets(db, _trojan(TROJAN))
            await _add_user(db, admin_id, f"{USERNAME}-second", prepared)
            await db.commit()
            return prepared.trojan.password

    async with sessions() as first:
        prepared = await enforce_unique_proxy_secrets(first, _trojan(TROJAN))
        await _add_user(first, admin_id, f"{USERNAME}-first", prepared)
        waiting = asyncio.create_task(second_writer())
        await connected.wait()
        await asyncio.sleep(WAIT_SECONDS)
        second_was_waiting = not waiting.done()
        await first.commit()

    second_value = await waiting

    assert prepared.trojan.password == TROJAN
    assert second_was_waiting is True
    assert second_value != TROJAN
    assert (await _holders(sessions, ProxySecretField.trojan_password))[TROJAN] == 1


@pytest.mark.asyncio
async def test_concurrent_creates_with_the_same_supplied_value_store_it_once(engine, sessions, admin_id):
    writers = 10 if engine.dialect.name == "sqlite" else 50

    async def create(index):
        async with sessions() as db:
            prepared = await _operation()._prepare_user_proxy_settings(db, [], ProxyTable(vless={"id": VLESS_ID}))
            await asyncio.sleep(CHECK_TO_WRITE_SECONDS)
            await _add_user(db, admin_id, f"{USERNAME}{index:03d}", prepared)
            await db.commit()

    await asyncio.gather(*(create(index) for index in range(writers)))

    holders = await _holders(sessions, ProxySecretField.vless_id)
    assert sum(holders.values()) == writers
    assert holders[VLESS_ID] == 1
    assert max(holders.values()) == 1


@pytest.mark.asyncio
async def test_a_missing_lock_row_is_seeded_on_first_use(engine, sessions):
    from app.fork.models.named_lock import NamedLock
    from app.fork.proxy_secrets.locks import PROXY_SECRETS_LOCK, acquire_named_lock

    table = NamedLock.__table__
    async with sessions() as db:
        await db.execute(delete(table).where(table.c.name == PROXY_SECRETS_LOCK))
        await db.commit()

    statements = []
    event.listen(
        engine.sync_engine,
        "before_cursor_execute",
        lambda conn, cursor, statement, *rest: statements.append(statement) if "named_locks" in statement else None,
    )
    async with sessions() as db:
        await acquire_named_lock(db, PROXY_SECRETS_LOCK)
        taken = len(statements)
        await acquire_named_lock(db, PROXY_SECRETS_LOCK)
        taken_again = len(statements)
        await db.commit()

    async with sessions() as db:
        names = (await db.execute(select(table.c.name).where(table.c.name == PROXY_SECRETS_LOCK))).scalars().all()

    assert names == [PROXY_SECRETS_LOCK]
    assert taken >= 2
    assert taken_again == taken


async def _short_lock_timeout(db):
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        await db.execute(text("PRAGMA busy_timeout = 1000"))
    elif dialect == "postgresql":
        await db.execute(text("SET lock_timeout = '1s'"))
    else:
        await db.execute(text("SET SESSION innodb_lock_wait_timeout = 1"))


@pytest.mark.asyncio
async def test_a_create_that_cannot_take_the_lock_in_time_answers_503_and_stores_nothing(sessions, admin_id):
    from app.fork.proxy_secrets.locks import PROXY_SECRETS_LOCK, acquire_named_lock

    async with sessions() as holder:
        await acquire_named_lock(holder, PROXY_SECRETS_LOCK)

        async with sessions() as victim:
            await _short_lock_timeout(victim)
            db_admin = await victim.get(Admin, admin_id)
            with pytest.raises(HTTPException) as raised:
                await _operation()._persist_bulk_users(
                    victim,
                    SimpleNamespace(is_owner=True),
                    db_admin,
                    [UserCreate(username=f"{USERNAME}-late", proxy_settings={"trojan": {"password": TROJAN}})],
                    [],
                    skip_per_user_limits=True,
                    sync=False,
                )
        await holder.rollback()

    assert raised.value.status_code == 503
    async with sessions() as db:
        stored = (await db.execute(select(User.id).where(User.username == f"{USERNAME}-late"))).all()
    assert stored == []
