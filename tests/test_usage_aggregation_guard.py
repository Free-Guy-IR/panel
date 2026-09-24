import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import column, select, table
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import OperationalError

import app.operation.user as user_operation
from app.fork.operation import usage_guard
from app.models.admin import AdminDetails, AdminRoleData
from app.models.stats import Period
from app.models.user import UsersUsageBreakdownQuery
from app.operation import OperatorType
from app.operation.user import UserOperation

USAGE = table("node_user_usages", column("used_traffic"))


def _owner(admin_id: int) -> AdminDetails:
    return AdminDetails(id=admin_id, username=f"owner{admin_id}", role=AdminRoleData(is_owner=True))


def _query() -> UsersUsageBreakdownQuery:
    now = datetime.now(UTC)
    return UsersUsageBreakdownQuery(start=now - timedelta(days=1), end=now, period=Period.hour)


@pytest.mark.asyncio
@pytest.mark.parametrize("crud_name", ["get_all_users_usages", "get_user_count_metric_stats"])
async def test_a_second_aggregation_for_the_same_admin_is_refused_while_the_first_runs(monkeypatch, crud_name):
    started = asyncio.Event()
    released = asyncio.Event()

    async def slow_aggregate(**_kwargs):
        started.set()
        await released.wait()
        return "result"

    monkeypatch.setattr(user_operation, crud_name, slow_aggregate)
    operation = UserOperation(OperatorType.API)

    async def run(admin: AdminDetails):
        if crud_name == "get_all_users_usages":
            return await operation.get_users_usage(None, admin, _query())
        return await operation.get_users_count_metric(None, admin, "online", _query())

    first = asyncio.create_task(run(_owner(1)))
    await asyncio.wait_for(started.wait(), timeout=5)
    with pytest.raises(HTTPException) as refused:
        await asyncio.wait_for(run(_owner(1)), timeout=5)
    assert refused.value.status_code == 429

    other_admin = asyncio.create_task(run(_owner(2)))
    await asyncio.sleep(0)
    released.set()
    assert await first == "result"
    assert await other_admin == "result"
    started.clear()
    released.set()
    assert await run(_owner(1)) == "result"


class _RecordingSession:
    def __init__(self, dialect: str, error: Exception | None = None):
        self.bind = SimpleNamespace(dialect=SimpleNamespace(name=dialect))
        self.statements = []
        self.rolled_back = False
        self.error = error

    async def execute(self, statement, *_args):
        self.statements.append(statement)
        if self.error is not None and len(self.statements) == 1 + (self.bind.dialect.name == "postgresql"):
            raise self.error
        return "rows"

    async def rollback(self):
        self.rolled_back = True


@pytest.mark.asyncio
async def test_mysql_aggregations_carry_an_execution_time_hint():
    db = _RecordingSession("mysql")
    assert await usage_guard.execute_with_statement_timeout(db, select(USAGE.c.used_traffic)) == "rows"
    compiled = str(db.statements[0].compile(dialect=mysql.dialect()))
    assert compiled.startswith(f"SELECT /*+ MAX_EXECUTION_TIME({usage_guard.USAGE_STATEMENT_TIMEOUT_MS}) */")


@pytest.mark.asyncio
async def test_postgres_aggregations_set_a_transaction_local_statement_timeout():
    db = _RecordingSession("postgresql")
    await usage_guard.execute_with_statement_timeout(db, select(USAGE.c.used_traffic))
    assert str(db.statements[0]) == f"SET LOCAL statement_timeout = {usage_guard.USAGE_STATEMENT_TIMEOUT_MS}"


@pytest.mark.asyncio
async def test_a_timed_out_aggregation_is_a_503_and_rolls_back():
    killed = OperationalError("SELECT", {}, Exception(usage_guard.MYSQL_STATEMENT_TIMEOUT_ERROR, "interrupted"))
    db = _RecordingSession("mysql", error=killed)
    with pytest.raises(HTTPException) as raised:
        await usage_guard.execute_with_statement_timeout(db, select(USAGE.c.used_traffic))
    assert raised.value.status_code == 503
    assert db.rolled_back


@pytest.mark.asyncio
async def test_other_database_errors_are_not_disguised():
    broken = OperationalError("SELECT", {}, Exception(2013, "lost connection"))
    db = _RecordingSession("mysql", error=broken)
    with pytest.raises(OperationalError):
        await usage_guard.execute_with_statement_timeout(db, select(USAGE.c.used_traffic))
    assert not db.rolled_back


@pytest.mark.asyncio
async def test_the_usage_chart_and_the_count_chart_of_one_admin_load_side_by_side(monkeypatch):
    started = asyncio.Event()
    released = asyncio.Event()

    async def slow_usage(**_kwargs):
        started.set()
        await released.wait()
        return "usage"

    async def quick_count(**_kwargs):
        return "count"

    monkeypatch.setattr(user_operation, "get_all_users_usages", slow_usage)
    monkeypatch.setattr(user_operation, "get_user_count_metric_stats", quick_count)
    operation = UserOperation(OperatorType.API)

    usage = asyncio.create_task(operation.get_users_usage(None, _owner(1), _query()))
    await asyncio.wait_for(started.wait(), timeout=5)
    assert await operation.get_users_count_metric(None, _owner(1), "online", _query()) == "count"
    released.set()
    assert await usage == "usage"
