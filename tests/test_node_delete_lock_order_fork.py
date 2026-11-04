from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, func, select, text, update
from sqlalchemy.exc import OperationalError

from app.db.crud import node as node_crud
from app.db.crud.node import remove_node, remove_nodes
from app.db.models import Node, NodeOutboundUsage
from app.fork.crud import node as fork_node_crud
from tests import test_record_outbound_usage_fork as outbound_tests, test_record_usages as recorder_tests

session_factory = recorder_tests.session_factory


class _DriverError(Exception):
    def __init__(self, *args, sqlstate=None):
        super().__init__(*args)
        self.sqlstate = sqlstate


def _mysql_deadlock() -> OperationalError:
    return OperationalError("DELETE FROM nodes", {}, _DriverError(1213, "Deadlock found when trying to get lock"))


def _serialization_failure() -> OperationalError:
    return OperationalError("DELETE FROM nodes", {}, _DriverError("could not serialize access", sqlstate="40001"))


def _missing_parent() -> OperationalError:
    return OperationalError("DELETE FROM nodes", {}, _DriverError(1452, "Cannot add or update a child row"))


def _fail_node_delete(monkeypatch, session, errors: list):
    real_execute = session.execute
    calls = {"node_deletes": 0}

    async def execute(statement, *args, **kwargs):
        if str(statement).startswith("DELETE FROM nodes"):
            calls["node_deletes"] += 1
            if errors:
                raise errors.pop(0)()
        return await real_execute(statement, *args, **kwargs)

    monkeypatch.setattr(session, "execute", execute)
    monkeypatch.setattr(fork_node_crud, "NODE_DELETE_BACKOFF_SECONDS", 0)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("injected", [_mysql_deadlock, _serialization_failure])
async def test_remove_node_retries_a_deadlock_once_and_deletes_the_node(monkeypatch, session_factory, injected):
    doomed_id, kept_id = await outbound_tests._seed_two_nodes(session_factory)
    async with session_factory() as session:
        calls = _fail_node_delete(monkeypatch, session, [injected])
        await remove_node(session, await session.get(Node, doomed_id))
    assert calls["node_deletes"] == 2
    async with session_factory() as session:
        assert await session.get(Node, doomed_id) is None
        assert await session.get(Node, kept_id) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("injected", [_mysql_deadlock, _serialization_failure])
async def test_remove_nodes_retries_a_deadlock_once_and_deletes_the_nodes(monkeypatch, session_factory, injected):
    doomed_id, kept_id = await outbound_tests._seed_two_nodes(session_factory)
    async with session_factory() as session:
        calls = _fail_node_delete(monkeypatch, session, [injected])
        await remove_nodes(session, [kept_id, doomed_id])
    assert calls["node_deletes"] == 2
    async with session_factory() as session:
        assert (await session.execute(select(func.count()).select_from(Node))).scalar_one() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("remove", ["one", "many"])
async def test_node_delete_gives_up_after_the_bounded_attempts_with_a_clear_error(monkeypatch, session_factory, remove):
    doomed_id, _ = await outbound_tests._seed_two_nodes(session_factory)
    attempts = node_crud.NODE_DELETE_ATTEMPTS
    async with session_factory() as session:
        calls = _fail_node_delete(monkeypatch, session, [_mysql_deadlock] * (attempts + 5))
        with pytest.raises(node_crud.NodeDeleteConflict, match=rf"gave up after {attempts} attempts") as raised:
            if remove == "one":
                await remove_node(session, await session.get(Node, doomed_id))
            else:
                await remove_nodes(session, [doomed_id])
    assert calls["node_deletes"] == attempts
    assert isinstance(raised.value.__cause__, OperationalError)
    async with session_factory() as session:
        assert await session.get(Node, doomed_id) is not None


@pytest.mark.asyncio
async def test_node_delete_does_not_retry_an_error_that_is_not_a_deadlock(monkeypatch, session_factory):
    doomed_id, _ = await outbound_tests._seed_two_nodes(session_factory)
    async with session_factory() as session:
        calls = _fail_node_delete(monkeypatch, session, [_missing_parent])
        with pytest.raises(OperationalError):
            await remove_node(session, await session.get(Node, doomed_id))
    assert calls["node_deletes"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("remove", ["one", "many"])
async def test_node_delete_locks_the_node_rows_in_id_order_before_touching_any_child_table(session_factory, remove):
    doomed_id, kept_id = await outbound_tests._seed_two_nodes(session_factory)
    async with session_factory() as session:
        sync_engine = session.bind.sync_engine
        statements: list[str] = []

        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(" ".join(statement.split()))

        event.listen(sync_engine, "before_cursor_execute", record)
        try:
            if remove == "one":
                await remove_node(session, await session.get(Node, doomed_id))
            else:
                await remove_nodes(session, [kept_id, doomed_id])
        finally:
            event.remove(sync_engine, "before_cursor_execute", record)
    writes = [index for index, sql in enumerate(statements) if sql.startswith(("DELETE", "UPDATE", "INSERT"))]
    locks = [index for index, sql in enumerate(statements) if sql.startswith("SELECT nodes.id FROM nodes")]
    assert locks, statements
    assert locks[0] < writes[0], statements
    assert "ORDER BY nodes.id" in statements[locks[0]]
    if session.bind.dialect.name != "sqlite":
        assert "FOR UPDATE" in statements[locks[0]]


@pytest.mark.asyncio
async def test_node_delete_waits_for_the_usage_writer_lock_and_never_deadlocks_on_a_real_database(
    monkeypatch, session_factory
):
    async with session_factory() as session:
        dialect_name = session.bind.dialect.name
        engine = session.bind
    if dialect_name == "sqlite":
        pytest.skip("needs a real database with row locks and concurrent writers")
    lock_waits_sql = text(
        "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'"
        if dialect_name == "postgresql"
        else "SELECT count(*) FROM information_schema.innodb_trx WHERE trx_state = 'LOCK WAIT'"
    )
    doomed_id, kept_id = await outbound_tests._seed_two_nodes(session_factory)
    retries = []
    real_sleep = asyncio.sleep

    async def counting_sleep(delay, *args, **kwargs):
        retries.append(delay)
        return await real_sleep(delay, *args, **kwargs)

    monkeypatch.setattr(fork_node_crud, "NODE_DELETE_BACKOFF_SECONDS", 0.001)
    monkeypatch.setattr(fork_node_crud, "asyncio", type("CountingAsyncio", (), {"sleep": staticmethod(counting_sleep)}))
    writer_engine = (
        engine.execution_options(isolation_level="READ COMMITTED") if dialect_name != "postgresql" else engine
    )
    async with writer_engine.connect() as writer:
        transaction = await writer.begin()
        await writer.execute(update(Node).where(Node.id.in_([doomed_id, kept_id])).values(uplink=Node.uplink + 1))
        remover = asyncio.create_task(outbound_tests._delete_node(session_factory, doomed_id))
        blocked = False
        for _ in range(100):
            await real_sleep(0.05)
            async with session_factory() as probe:
                if (await probe.execute(lock_waits_sql)).scalar_one() > 0:
                    blocked = True
                    break
        assert blocked and not remover.done()
        await writer.execute(
            NodeOutboundUsage.__table__.insert(),
            [
                {
                    "node_id": doomed_id,
                    "outbound_tag": "direct",
                    "uplink": 3,
                    "downlink": 4,
                    "created_at": datetime.now(UTC).replace(tzinfo=None, minute=0, second=0, microsecond=0),
                }
            ],
        )
        await transaction.commit()
    await asyncio.wait_for(remover, timeout=30)
    assert retries == []
    async with session_factory() as session:
        assert await session.get(Node, doomed_id) is None
        assert (await session.get(Node, kept_id)).uplink == 1
        assert (
            await session.execute(
                select(func.count()).select_from(NodeOutboundUsage).where(NodeOutboundUsage.node_id == doomed_id)
            )
        ).scalar_one() == 0
