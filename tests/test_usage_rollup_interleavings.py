from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event, insert, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.sql.dml import Delete, Insert, Update
from sqlalchemy.sql.elements import TextClause

from app.db import base
from app.db.crud import bulk as bulk_crud, node as node_crud, user as user_crud
from app.db.models import Admin, AdminRole, Node, NodeOutboundUsage, NodeUsage, NodeUserUsage, User, UserStatus
from app.fork.crud import node as fork_node_crud
from app.fork.migration_guards import drop_migration_owned_tables
from app.jobs import usage_rollup
from app.models.proxy import ProxyTable
from app.models.settings import CleanupSettings
from config import database_settings
from tests.test_recorder_harness import restore_seeded_rows, snapshot_seeded_rows

SERVER = os.getenv("TEST_FROM", "local").lower() != "local"
server_database_only = pytest.mark.skipif(not SERVER, reason="needs a server database with row locks and deadlocks")
SETTINGS = CleanupSettings(usage_rollup_hourly_after_days=2)
TRAFFIC = (100, 7, 3)
SLOTS = 6
WAIT_LIMIT = 60
WAITERS = {
    "postgresql": (
        "select count(*) from pg_stat_activity where wait_event_type = 'Lock' and datname = current_database()"
    ),
    "mysql": (
        "select count(*) from information_schema.processlist "
        "where command = 'Query' and time >= 1 and id <> connection_id() and db = database()"
    ),
}


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _bucket() -> datetime:
    return (datetime.now(UTC) - timedelta(days=5)).replace(minute=0, second=0, microsecond=0)


@pytest.fixture(name="world")
async def world_fixture(tmp_path):
    if SERVER:
        url = database_settings.url
        engine = create_async_engine(url, poolclass=NullPool)
    else:
        url = f"sqlite+aiosqlite:///{tmp_path / 'rollup.db'}"
        engine = create_async_engine(url, poolclass=NullPool, connect_args={"check_same_thread": False})

        @event.listens_for(engine.sync_engine, "connect")
        def _pragmas(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    json_column = base.Base.metadata.tables["users"].c.proxy_settings
    json_default = json_column.server_default
    if url.startswith("mysql"):
        json_column.server_default = None
    seeded = await snapshot_seeded_rows(engine) if SERVER else None
    async with engine.begin() as conn:
        await conn.run_sync(drop_migration_owned_tables)
        await conn.run_sync(base.Base.metadata.drop_all)
        await conn.run_sync(base.Base.metadata.create_all)

    yield SimpleNamespace(
        engine=engine, factory=async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    )

    async with engine.begin() as conn:
        await conn.run_sync(drop_migration_owned_tables)
        await conn.run_sync(base.Base.metadata.drop_all)
        await conn.run_sync(base.Base.metadata.create_all)
    if seeded is not None:
        await restore_seeded_rows(engine, seeded)
    await engine.dispose()
    json_column.server_default = json_default


async def _seed(world, *, users: int = 2, expired_first: bool = False, table=NodeUserUsage) -> tuple[list[int], int]:
    tag = uuid4().hex[:10]
    async with world.factory() as db:
        role = AdminRole(name=f"rollup-{tag}", is_owner=True, permissions={}, limits={}, features={}, access={})
        db.add(role)
        await db.flush()
        admin = Admin(username=f"rollup-admin-{tag}", hashed_password="secret", role_id=role.id)
        db.add(admin)
        await db.flush()
        node = Node(
            name=f"rollup-node-{tag}",
            address="10.88.0.1",
            port=7000,
            api_port=7001,
            server_ca="ca",
            api_key=f"key-{tag}",
            core_config_id=None,
        )
        rows = [
            User(username=f"rollup-{tag}-{index}", admin_id=admin.id, proxy_settings=ProxyTable().dict(no_obj=True))
            for index in range(users)
        ]
        db.add(node)
        db.add_all(rows)
        await db.flush()
        if expired_first:
            rows[0].status = UserStatus.expired
            rows[0].last_status_change = datetime.now(UTC) - timedelta(days=40)
            rows[0].auto_delete_in_days = 0
        user_ids = [row.id for row in rows]
        node_id = node.id
        await db.commit()

    bucket = _bucket()
    stamps = [bucket + timedelta(minutes=10 * slot) for slot in range(SLOTS)]
    if table is NodeUserUsage:
        values = [
            {"created_at": stamp, "user_id": user_id, "node_id": node_id, "used_traffic": TRAFFIC[index]}
            for stamp in stamps
            for index, user_id in enumerate(user_ids)
        ]
    elif table is NodeUsage:
        values = [{"created_at": stamp, "node_id": node_id, "uplink": 5, "downlink": 1} for stamp in stamps]
    else:
        values = [
            {"created_at": stamp, "node_id": node_id, "outbound_tag": "direct", "uplink": 5, "downlink": 1}
            for stamp in stamps
        ]
    async with world.factory() as db:
        await db.execute(insert(table), values)
        await db.commit()
    return user_ids, node_id


class Pause:
    def __init__(self, matches=None):
        self.matches = matches
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    async def _hold(self):
        self.reached.set()
        await self.release.wait()

    def before_statement(self, session):
        execute = session.execute

        async def gated(statement, *args, **kwargs):
            if not self.reached.is_set() and self.matches(statement):
                await self._hold()
            return await execute(statement, *args, **kwargs)

        session.execute = gated

    def before_commit(self, session):
        commit = session.commit

        async def gated():
            if not self.reached.is_set():
                await self._hold()
            return await commit()

        session.commit = gated


def _deletes_from(model):
    return lambda statement: isinstance(statement, Delete) and statement.table.name == model.__tablename__


def _inserts_into(model):
    return lambda statement: isinstance(statement, Insert) and statement.table.name == model.__tablename__


def _signal_first_write(session, signal: asyncio.Event) -> None:
    execute = session.execute

    async def watched(statement, *args, **kwargs):
        if isinstance(statement, (Delete, Insert, Update)) or (
            isinstance(statement, TextClause) and statement.text.upper().startswith("BEGIN")
        ):
            signal.set()
        return await execute(statement, *args, **kwargs)

    session.execute = watched


async def _rollup(world, *, pause: Pause | None = None, first_write: asyncio.Event | None = None):
    async with world.factory() as db:
        if pause is not None:
            pause.before_statement(db)
        if first_write is not None:
            _signal_first_write(db, first_write)
        return await usage_rollup.rollup_usage_history(db, SETTINGS, datetime.now(UTC))


async def _reset(db, user_id: int) -> None:
    db_user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
    await user_crud.reset_user_data_usage(db, db_user, clean_chart_data=True)


async def _reset_all(db, user_id: int) -> None:
    await bulk_crud.reset_all_users_data_usage(db, admin=None, clean_chart_data=True)


async def _remove(db, user_id: int) -> None:
    db_user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
    await user_crud.remove_users(db, [db_user])


async def _autodelete(db, user_id: int) -> None:
    await user_crud.autodelete_expired_users(db)


PARTNERS = {"reset": _reset, "reset_all": _reset_all, "remove": _remove, "autodelete": _autodelete}


async def _partner(world, name: str, user_id: int, *, commit_pause: Pause | None = None, no_wait: bool = False):
    async with world.factory() as db:
        if no_wait:
            await db.execute(text("PRAGMA busy_timeout = 0"))
        if commit_pause is not None:
            commit_pause.before_commit(db)
        await PARTNERS[name](db, user_id)
    return name


async def _waits_on_a_lock(world, task: asyncio.Task) -> bool:
    name = world.engine.dialect.name
    query = text(WAITERS["postgresql" if name == "postgresql" else "mysql"])
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WAIT_LIMIT
    while loop.time() < deadline:
        if task.done():
            return False
        async with world.engine.connect() as conn:
            if (await conn.execute(query)).scalar_one():
                return True
        await asyncio.sleep(0.01)
    raise AssertionError("the overlapping writer neither waited on a lock nor finished")


async def _settle(pause: Pause | None, *tasks: asyncio.Task) -> list:
    if pause is not None:
        pause.release.set()
    return await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), WAIT_LIMIT)


async def _partner_inside_the_window(world, name: str, user_id: int, pause: Pause, rollup_done: asyncio.Event):
    if SERVER:
        writer = asyncio.create_task(_partner(world, name, user_id))
        try:
            await _waits_on_a_lock(world, writer)
        finally:
            pause.release.set()
        return [writer]
    try:
        await _partner(world, name, user_id, no_wait=True)
    except OperationalError as exc:
        assert "database is locked" in str(exc), exc
        return [asyncio.create_task(_partner_after(world, name, user_id, rollup_done))]
    finally:
        pause.release.set()
    return []


async def _partner_after(world, name: str, user_id: int, rollup_done: asyncio.Event) -> str:
    await rollup_done.wait()
    return await _partner(world, name, user_id)


async def _charts(world, user_ids: list[int]) -> dict[int, list[tuple[datetime, int]]]:
    async with world.factory() as db:
        rows = (
            await db.execute(
                select(NodeUserUsage.user_id, NodeUserUsage.created_at, NodeUserUsage.used_traffic)
                .where(NodeUserUsage.user_id.in_(user_ids))
                .order_by(NodeUserUsage.user_id, NodeUserUsage.created_at)
            )
        ).all()
    charts = {user_id: [] for user_id in user_ids}
    for user_id, created_at, used_traffic in rows:
        charts[user_id].append((_utc(created_at), used_traffic))
    return charts


async def _existing(world, user_ids: list[int]) -> set[int]:
    async with world.factory() as db:
        return set((await db.execute(select(User.id).where(User.id.in_(user_ids)))).scalars())


def _failures(results: list) -> list:
    return [result for result in results if isinstance(result, BaseException)]


async def _run_rollup_with_partner_in_window(world, name: str, user_id: int, matches) -> list:
    pause = Pause(matches)
    rollup_done = asyncio.Event()

    async def rollup():
        try:
            return await _rollup(world, pause=pause)
        finally:
            rollup_done.set()

    rollup_task = asyncio.create_task(rollup())
    writers: list[asyncio.Task] = []
    try:
        await asyncio.wait_for(pause.reached.wait(), WAIT_LIMIT)
        writers = await _partner_inside_the_window(world, name, user_id, pause, rollup_done)
    finally:
        results = await _settle(pause, rollup_task, *writers)
    return results


@pytest.mark.asyncio
@pytest.mark.parametrize("partner", ["reset", "reset_all"])
async def test_a_chart_reset_inside_the_rollup_read_window_is_not_undone(world, partner):
    user_ids, _ = await _seed(world, users=2)

    results = await _run_rollup_with_partner_in_window(world, partner, user_ids[0], _deletes_from(NodeUserUsage))

    assert not _failures(results), results
    charts = await _charts(world, user_ids)
    if partner == "reset_all":
        assert charts == {user_id: [] for user_id in user_ids}, charts
    else:
        assert charts == {user_ids[0]: [], user_ids[1]: [(_bucket(), TRAFFIC[1] * SLOTS)]}, charts


@pytest.mark.asyncio
@pytest.mark.parametrize("partner", ["remove", "autodelete"])
async def test_a_user_delete_inside_the_rollup_read_window_neither_fails_the_rollup_nor_loses_work(world, partner):
    user_ids, _ = await _seed(world, users=2, expired_first=partner == "autodelete")

    results = await _run_rollup_with_partner_in_window(world, partner, user_ids[0], _deletes_from(NodeUserUsage))

    assert not _failures(results), results
    if partner == "autodelete":
        await _partner(world, partner, user_ids[0])
    assert await _existing(world, user_ids) == {user_ids[1]}
    assert await _charts(world, user_ids) == {user_ids[0]: [], user_ids[1]: [(_bucket(), TRAFFIC[1] * SLOTS)]}


@pytest.mark.asyncio
@pytest.mark.parametrize("partner", ["reset", "reset_all"])
async def test_a_chart_reset_in_flight_when_the_rollup_reads_is_not_undone(world, partner):
    user_ids, _ = await _seed(world, users=2)
    commit_pause = Pause()
    writer = asyncio.create_task(_partner(world, partner, user_ids[0], commit_pause=commit_pause))
    rollup_task = None
    try:
        await asyncio.wait_for(commit_pause.reached.wait(), WAIT_LIMIT)
        if SERVER:
            rollup_task = asyncio.create_task(_rollup(world))
            assert await _waits_on_a_lock(world, rollup_task), "the rollup did not wait for the reset"
        else:
            first_write = asyncio.Event()
            rollup_task = asyncio.create_task(_rollup(world, first_write=first_write))
            await asyncio.wait_for(first_write.wait(), WAIT_LIMIT)
    finally:
        results = await _settle(commit_pause, writer, *([rollup_task] if rollup_task else []))

    assert not _failures(results), results
    charts = await _charts(world, user_ids)
    if partner == "reset_all":
        assert charts == {user_id: [] for user_id in user_ids}, charts
    else:
        assert charts == {user_ids[0]: [], user_ids[1]: [(_bucket(), TRAFFIC[1] * SLOTS)]}, charts


@pytest.mark.asyncio
@pytest.mark.parametrize("partner", ["reset", "remove", "autodelete"])
async def test_the_rollup_write_and_a_user_writer_neither_deadlock_nor_lose_work(world, partner):
    user_ids, _ = await _seed(world, users=2, expired_first=partner == "autodelete")

    results = await _run_rollup_with_partner_in_window(world, partner, user_ids[0], _inserts_into(NodeUserUsage))

    assert not _failures(results), results
    if partner == "autodelete":
        await _partner(world, partner, user_ids[0])
    charts = await _charts(world, user_ids)
    assert charts[user_ids[1]] == [(_bucket(), TRAFFIC[1] * SLOTS)], charts
    assert charts[user_ids[0]] == [], charts
    expected = set(user_ids) if partner == "reset" else {user_ids[1]}
    assert await _existing(world, user_ids) == expected


async def _delete_node(world, node_id: int) -> str:
    async with world.factory() as db:
        db_node = (await db.execute(select(Node).where(Node.id == node_id))).scalar_one()
        await node_crud.remove_node(db, db_node)
    return "node deleted"


@server_database_only
@pytest.mark.asyncio
@pytest.mark.parametrize("model", [NodeUserUsage, NodeUsage, NodeOutboundUsage], ids=lambda model: model.__tablename__)
async def test_the_rollup_write_and_a_node_delete_neither_deadlock_nor_lose_work(world, monkeypatch, model):
    monkeypatch.setattr(fork_node_crud, "NODE_DELETE_ATTEMPTS", 1)
    _, node_id = await _seed(world, users=1, table=model)
    pause = Pause(_inserts_into(model))
    rollup_task = asyncio.create_task(_rollup(world, pause=pause))
    deleter = None
    try:
        await asyncio.wait_for(pause.reached.wait(), WAIT_LIMIT)
        deleter = asyncio.create_task(_delete_node(world, node_id))
        await _waits_on_a_lock(world, deleter)
    finally:
        results = await _settle(pause, rollup_task, *([deleter] if deleter else []))

    assert not _failures(results), results
    async with world.factory() as db:
        assert (await db.execute(select(Node.id).where(Node.id == node_id))).first() is None
        stamps = (await db.execute(select(model.created_at, model.node_id))).all()
    expected = [] if model is NodeOutboundUsage else [(_bucket(), None)]
    assert [(_utc(created_at), owner) for created_at, owner in stamps] == expected
