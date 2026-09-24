import asyncio
import os
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import delete, event, func, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.crud.user import get_user_by_id, reset_user_by_next
from app.db.models import (
    Admin,
    AdminRole,
    Base,
    CoreConfig,
    Group,
    NextPlan,
    ProxyInbound,
    User,
    UserTemplate,
    inbounds_groups_association,
    template_group_association,
    users_groups_association,
)
from app.fork.proxy_secrets import SPEC_BY_FIELD, ProxySecretField, enforce_unique_proxy_secrets, secret_column
from app.models.core import CoreType
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
DEADLOCK_SETUP_SECONDS = 0.3
SEED_RACE_WRITERS = 6
SEED_RACE_ROUNDS = 5


def _foreign_keys_on(dbapi_connection, _record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


async def _purge(engine):
    async with engine.begin() as conn:
        user_ids = select(User.id).where(User.username.like(f"{USERNAME}%")).scalar_subquery()
        group_ids = select(Group.id).where(Group.name.like(f"{PREFIX}%")).scalar_subquery()
        template_ids = select(UserTemplate.id).where(UserTemplate.name.like(f"{PREFIX}%")).scalar_subquery()
        await conn.execute(delete(users_groups_association).where(users_groups_association.c.user_id.in_(user_ids)))
        await conn.execute(delete(User.__table__).where(User.username.like(f"{USERNAME}%")))
        await conn.execute(
            delete(template_group_association).where(template_group_association.c.user_template_id.in_(template_ids))
        )
        await conn.execute(delete(UserTemplate.__table__).where(UserTemplate.name.like(f"{PREFIX}%")))
        await conn.execute(
            delete(inbounds_groups_association).where(inbounds_groups_association.c.group_id.in_(group_ids))
        )
        await conn.execute(delete(Group.__table__).where(Group.name.like(f"{PREFIX}%")))
        await conn.execute(delete(ProxyInbound.__table__).where(ProxyInbound.tag.like(f"{PREFIX}%")))
        await conn.execute(delete(CoreConfig.__table__).where(CoreConfig.name.like(f"{PREFIX}%")))
        await conn.execute(delete(Admin.__table__).where(Admin.username.like(f"{PREFIX}%")))
        await conn.execute(delete(AdminRole.__table__).where(AdminRole.name.like(f"{PREFIX}%")))


async def _seed_lock_row(engine):
    from app.fork.proxy_secrets.locks import PROXY_SECRETS_LOCK, named_lock_seed_statement

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
    assert taken >= 1
    assert taken_again == taken


@pytest.mark.asyncio
async def test_the_lock_holder_never_needs_a_second_slot_from_a_full_pool(engine, sessions, admin_id):
    writers = 3
    bounded = create_async_engine(engine.url, pool_size=writers, max_overflow=0, pool_timeout=1)
    bounded_sessions = async_sessionmaker(bounded, class_=AsyncSession, autoflush=False, expire_on_commit=False)
    everyone_connected = asyncio.Barrier(writers)

    async def create(index):
        async with bounded_sessions() as db:
            await db.execute(select(1))
            await everyone_connected.wait()
            prepared = await enforce_unique_proxy_secrets(db, _trojan(TROJAN))
            await _add_user(db, admin_id, f"{USERNAME}-pool{index}", prepared)
            await db.commit()

    try:
        outcomes = await asyncio.gather(*(create(index) for index in range(writers)), return_exceptions=True)
    finally:
        await bounded.dispose()

    assert [f"{type(outcome).__name__}: {outcome}" for outcome in outcomes if outcome is not None] == []
    assert (await _holders(sessions, ProxySecretField.trojan_password))[TROJAN] == 1


@pytest.mark.skipif(
    not database_settings.url.startswith(MYSQL_FAMILY), reason="the separate committed reader exists only on MySQL"
)
def test_the_committed_reader_is_not_tied_to_one_event_loop():
    from app.fork.proxy_secrets.locks import read_committed_rows

    engine = create_async_engine(database_settings.url, poolclass=NullPool)

    async def read():
        async with AsyncSession(engine) as db:
            return await read_committed_rows(db, select(1))

    try:
        first = asyncio.run(read())
        second = asyncio.run(read())
    finally:
        asyncio.run(engine.dispose())

    assert [tuple(row) for row in first] == [tuple(row) for row in second] == [(1,)]


@pytest.mark.asyncio
async def test_writers_racing_on_a_deleted_lock_row_all_succeed_and_seed_it_once(engine, sessions, admin_id):
    from app.fork.models.named_lock import NamedLock
    from app.fork.proxy_secrets.locks import PROXY_SECRETS_LOCK

    table = NamedLock.__table__

    async def race(round_number):
        async with sessions() as db:
            await db.execute(delete(table).where(table.c.name == PROXY_SECRETS_LOCK))
            await db.commit()
        everyone_connected = asyncio.Barrier(SEED_RACE_WRITERS)

        async def create(index):
            async with sessions() as db:
                await db.execute(select(1))
                await everyone_connected.wait()
                prepared = await _operation()._prepare_user_proxy_settings(db, [], _trojan(TROJAN))
                await _add_user(db, admin_id, f"{USERNAME}-seed{round_number}-{index}", prepared)
                await db.commit()

        return await asyncio.gather(*(create(index) for index in range(SEED_RACE_WRITERS)), return_exceptions=True)

    failures = []
    for round_number in range(SEED_RACE_ROUNDS):
        outcomes = await race(round_number)
        failures += [f"{type(outcome).__name__}: {outcome}" for outcome in outcomes if outcome is not None]

    assert failures == []
    async with sessions() as db:
        names = (await db.execute(select(table.c.name).where(table.c.name == PROXY_SECRETS_LOCK))).scalars().all()
    assert names == [PROXY_SECRETS_LOCK]
    assert (await _holders(sessions, ProxySecretField.trojan_password))[TROJAN] == 1


@pytest.mark.asyncio
async def test_a_writer_chosen_as_deadlock_victim_answers_503_and_leaves_nothing_behind(engine, sessions, admin_id):
    from app.fork.proxy_secrets.locks import PROXY_SECRETS_LOCK, acquire_named_lock

    if engine.dialect.name == "sqlite":
        pytest.skip(reason="sqlite serializes writers and has no deadlock victims")
    async with sessions() as db:
        users = [
            User(username=f"{USERNAME}-{label}", proxy_settings=ProxyTable().dict(), admin_id=admin_id)
            for label in ("target", "ballast0", "ballast1", "ballast2", "ballast3", "ballast4")
        ]
        db.add_all(users)
        await db.commit()
        target_id, ballast_ids = users[0].id, [user.id for user in users[1:]]

    victim_holds_the_row = asyncio.Event()

    async def victim():
        async with sessions() as db:
            await db.execute(update(User).where(User.id == target_id).values(note="half-applied"))
            victim_holds_the_row.set()
            try:
                return await _operation()._prepare_user_proxy_settings(db, [], _trojan(TROJAN))
            finally:
                await db.commit()

    async with sessions() as holder:
        await acquire_named_lock(holder, PROXY_SECRETS_LOCK)
        await holder.execute(update(User).where(User.id.in_(ballast_ids)).values(note="ballast"))
        waiting = asyncio.create_task(victim())
        await victim_holds_the_row.wait()
        await asyncio.sleep(DEADLOCK_SETUP_SECONDS)
        await holder.execute(update(User).where(User.id == target_id).values(note="holder"))
        await holder.commit()

    with pytest.raises(HTTPException) as raised:
        await waiting

    assert raised.value.status_code == 503
    async with sessions() as db:
        note = await db.scalar(select(User.note).where(User.id == target_id))
    assert note == "holder"


class _DriverError(Exception):
    def __init__(self, code, message):
        super().__init__(code, message)
        self.sqlstate = None


@pytest.mark.parametrize(
    "failure",
    [
        OperationalError("UPDATE named_locks", {}, _DriverError(1213, "Deadlock found when trying to get lock")),
        OperationalError("UPDATE named_locks", {}, _DriverError(1205, "Lock wait timeout exceeded")),
    ],
    ids=["deadlock", "lock-wait-timeout"],
)
@pytest.mark.asyncio
async def test_a_lock_the_database_refused_answers_503(sessions, monkeypatch, failure):
    from app.fork.proxy_secrets import uniqueness

    async def refused(db):
        raise failure

    monkeypatch.setattr(uniqueness, "acquire_proxy_secrets_lock", refused)
    async with sessions() as db:
        with pytest.raises(HTTPException) as raised:
            await _operation()._prepare_user_proxy_settings(db, [], _trojan(TROJAN))

    assert raised.value.status_code == 503


@pytest.mark.asyncio
async def test_a_lock_row_deleted_under_the_writer_answers_503(sessions, monkeypatch):
    from app.fork.proxy_secrets import locks, uniqueness

    async def vanished(db):
        raise locks.NamedLockMissing("the 'proxy_secrets' named lock row could not be taken")

    monkeypatch.setattr(uniqueness, "acquire_proxy_secrets_lock", vanished)
    async with sessions() as db:
        with pytest.raises(HTTPException) as raised:
            await _operation()._prepare_user_proxy_settings(db, [], _trojan(TROJAN))

    assert raised.value.status_code == 503


@pytest.mark.asyncio
async def test_the_outside_seed_is_retried_once_after_a_deadlock():
    from app.fork.proxy_secrets import locks

    deadlock = OperationalError("INSERT INTO named_locks", {}, _DriverError(1213, "Deadlock found"))
    attempts = []

    class _Connection:
        async def execute(self, statement):
            attempts.append(statement)
            if len(attempts) == 1:
                raise deadlock

    class _Transaction:
        async def __aenter__(self):
            return _Connection()

        async def __aexit__(self, *exc_info):
            return False

    class _Engine:
        def begin(self):
            return _Transaction()

    await locks.seed_named_lock_outside(_Engine(), "mysql", locks.PROXY_SECRETS_LOCK)

    assert len(attempts) == 2


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


async def _seed_next_plan(sessions, admin_id):
    async with sessions() as db:
        inbound = ProxyInbound(tag=f"{PREFIX}ovpn")
        db.add(inbound)
        await db.flush()
        db.add(
            CoreConfig(name=f"{PREFIX}ovpn", type=CoreType.openvpn, config={"instances": [{"tag": f"{PREFIX}ovpn"}]})
        )
        plain = Group(name=f"{PREFIX}plain", inbounds=[])
        vpn = Group(name=f"{PREFIX}vpn", inbounds=[inbound])
        db.add_all([plain, vpn])
        await db.flush()
        template = UserTemplate(
            name=f"{PREFIX}plan",
            username_prefix=None,
            username_suffix=None,
            extra_settings=None,
            groups=[vpn],
            data_limit=5 * 1024**3,
            expire_duration=0,
        )
        db.add(template)
        await db.flush()
        user = User(username=f"{USERNAME}-renewing", proxy_settings=ProxyTable().dict(), admin_id=admin_id)
        user.groups = [plain]
        user.data_limit = 1024**3
        user.used_traffic = 1024**3
        db.add(user)
        await db.flush()
        db.add(NextPlan(user_id=user.id, user_template_id=template.id))
        await db.commit()
        return user.id


@pytest.mark.asyncio
async def test_a_next_plan_whose_grant_times_out_on_the_lock_is_still_applied(engine, sessions, admin_id):
    from app.fork.proxy_secrets.locks import PROXY_SECRETS_LOCK, acquire_named_lock

    if engine.dialect.name == "sqlite":
        pytest.skip(reason="sqlite locks the whole database, so the plan itself would wait for the holder")
    user_id = await _seed_next_plan(sessions, admin_id)

    async with sessions() as holder:
        await acquire_named_lock(holder, PROXY_SECRETS_LOCK)
        async with sessions() as reviewer:
            await _short_lock_timeout(reviewer)
            db_user = await get_user_by_id(reviewer, user_id)
            await reset_user_by_next(reviewer, db_user)
        await holder.rollback()

    async with sessions() as db:
        stored = await get_user_by_id(db, user_id)
        groups = sorted(group.name for group in stored.groups)
        openvpn_password = ProxyTable.model_validate(stored.proxy_settings).openvpn.password
        data_limit, used_traffic = stored.data_limit, stored.used_traffic

    assert groups == [f"{PREFIX}vpn"]
    assert data_limit == 5 * 1024**3
    assert used_traffic == 0
    assert openvpn_password is None
