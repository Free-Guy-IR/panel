from __future__ import annotations

import asyncio
import logging
import os
from collections import defaultdict
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import bindparam, select, update
from sqlalchemy.exc import DatabaseError, OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from app.db import base
from app.db.crud.bulk import reset_all_users_data_usage
from app.db.crud.user import bulk_reset_user_data_usage, reset_user_data_usage
from app.db.models import (
    Admin,
    AdminRole,
    AdminStatus,
    Node,
    NodeUsage,
    NodeUserUsage,
    System,
    User,
    UserUsageResetLogs,
)
from app.fork.usage_barrier import UsageApplyBarrier, usage_apply_barrier
from app.jobs import record_usages
from app.models.proxy import ProxyTable
from app.operation import admin_sync
from config import database_settings, runtime_settings


class DummyNode:
    def __init__(self, node_id: int, usage_coefficient: int = 1):
        self.node_id = node_id
        self._usage_coefficient = usage_coefficient

    async def get_extra(self) -> dict[str, Any]:
        return {"usage_coefficient": self._usage_coefficient}


def _get_test_database_url() -> str:
    test_from = os.getenv("TEST_FROM", "local").lower()
    if test_from == "local":
        return "sqlite+aiosqlite:///:memory:"
    return database_settings.url


@pytest.fixture
async def session_factory(monkeypatch: pytest.MonkeyPatch):
    database_url = _get_test_database_url()
    is_sqlite = database_url.startswith("sqlite")

    engine_kwargs = {}
    connect_args = {}
    if is_sqlite:
        connect_args["check_same_thread"] = False
        # Keep the in-memory database alive across connections
        engine_kwargs["poolclass"] = StaticPool
    else:
        engine_kwargs["poolclass"] = NullPool

    # MySQL/MariaDB do not allow defaults on JSON columns; strip them temporarily
    proxy_default = None
    proxy_column = None
    needs_json_default_fix = database_url.startswith("mysql")
    if needs_json_default_fix:
        users_table = base.Base.metadata.tables["users"]
        proxy_column = users_table.c.proxy_settings
        proxy_default = proxy_column.server_default
        proxy_column.server_default = None

    engine = create_async_engine(database_url, connect_args=connect_args, **engine_kwargs)
    async with engine.begin() as conn:
        await conn.run_sync(base.Base.metadata.drop_all)
        await conn.run_sync(base.Base.metadata.create_all)

    # Seed the 3 default roles so FK constraints on admins.role_id are satisfied
    async with async_sessionmaker(bind=engine, expire_on_commit=False)() as seed_session:
        seed_session.add_all(
            [
                AdminRole(name="owner", is_owner=True, permissions={}, limits={}, features={}, access={}),
                AdminRole(name="administrator", is_owner=False, permissions={}, limits={}, features={}, access={}),
                AdminRole(name="operator", is_owner=False, permissions={}, limits={}, features={}, access={}),
            ]
        )
        await seed_session.commit()

    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    class TestGetDB:
        def __init__(self):
            self.db = session_factory()

        async def __aenter__(self):
            return self.db

        async def __aexit__(self, exc_type, exc_value, traceback):
            if isinstance(exc_value, SQLAlchemyError):
                await self.db.rollback()
            await self.db.close()

    monkeypatch.setattr(record_usages, "engine", engine)
    monkeypatch.setattr(record_usages, "GetDB", TestGetDB)
    monkeypatch.setattr(admin_sync, "GetDB", TestGetDB)

    yield session_factory

    async with engine.begin() as conn:
        await conn.run_sync(base.Base.metadata.drop_all)
    await engine.dispose()
    if needs_json_default_fix and proxy_column is not None:
        proxy_column.server_default = proxy_default


@pytest.mark.asyncio
async def test_record_user_usages_updates_users_and_admins(monkeypatch: pytest.MonkeyPatch, session_factory):
    async with session_factory() as session:
        admin = Admin(username="admin", hashed_password="secret", role_id=3)
        session.add(admin)
        await session.flush()
        admin_id = admin.id

        user_one = User(username="user1", admin_id=admin_id, proxy_settings=ProxyTable().dict(no_obj=True))
        user_two = User(username="user2", admin_id=admin_id, proxy_settings=ProxyTable().dict(no_obj=True))
        session.add_all([user_one, user_two])
        await session.flush()
        user_one_id, user_two_id = user_one.id, user_two.id

        node_one = Node(
            name="node-1",
            address="10.0.0.1",
            port=1000,
            api_port=1001,
            server_ca="ca1",
            api_key="key1",
            core_config_id=None,
        )
        node_two = Node(
            name="node-2",
            address="10.0.0.2",
            port=1001,
            api_port=1002,
            server_ca="ca2",
            api_key="key2",
            core_config_id=None,
        )
        session.add_all([node_one, node_two])
        await session.flush()
        node_one_id, node_two_id = node_one.id, node_two.id
        await session.commit()

    nodes = [
        (node_one_id, DummyNode(node_one_id, usage_coefficient=2)),
        (node_two_id, DummyNode(node_two_id, usage_coefficient=1)),
    ]
    monkeypatch.setattr(record_usages.node_manager, "get_healthy_nodes", AsyncMock(return_value=nodes))

    stats_map = {
        node_one_id: [{"uid": str(user_one_id), "value": 100}, {"uid": str(user_two_id), "value": 50}],
        node_two_id: [{"uid": str(user_one_id), "value": 75}],
    }

    async def fake_get_users_stats(node: DummyNode, node_id: int | None = None):
        return stats_map[node.node_id]

    monkeypatch.setattr(record_usages, "get_users_stats", fake_get_users_stats)
    monkeypatch.setattr(record_usages.usage_settings, "disable_recording_node_usage", False)

    await record_usages.record_user_usages()

    async with session_factory() as session:
        users_result = await session.execute(
            select(User.id, User.used_traffic, User.online_at).where(User.id.in_([user_one_id, user_two_id]))
        )
        user_rows = users_result.all()
        user_totals = {row.id: (row.used_traffic, row.online_at) for row in user_rows}

        assert user_totals[user_one_id][0] > user_totals[user_two_id][0]
        assert all(total > 0 for total, _ in user_totals.values())
        assert all(online_at is not None for _, online_at in user_totals.values())

        admin_total = await session.execute(select(Admin.used_traffic).where(Admin.id == admin_id))
        admin_used = admin_total.scalar_one()
        assert admin_used == sum(total for total, _ in user_totals.values())

        node_usage_rows = await session.execute(
            select(NodeUserUsage.node_id, NodeUserUsage.user_id, NodeUserUsage.used_traffic)
        )
        node_usage_records = node_usage_rows.all()
        usage_pairs = {(row.node_id, row.user_id) for row in node_usage_records}
        assert usage_pairs == {
            (node_one_id, user_one_id),
            (node_one_id, user_two_id),
            (node_two_id, user_one_id),
        }

        aggregated_usage = defaultdict(int)
        for record in node_usage_records:
            assert record.used_traffic > 0
            aggregated_usage[record.user_id] += record.used_traffic

        for user_id, (total_usage, _) in user_totals.items():
            assert aggregated_usage[user_id] == total_usage


@pytest.mark.asyncio
async def test_record_user_usages_limits_overused_admin(monkeypatch: pytest.MonkeyPatch, session_factory):
    async with session_factory() as session:
        admin = Admin(username="limited-admin", hashed_password="secret", role_id=3, data_limit=100)
        session.add(admin)
        await session.flush()
        admin_id = admin.id

        user = User(username="limited-user", admin_id=admin_id, proxy_settings=ProxyTable().dict(no_obj=True))
        node = Node(
            name="node-1",
            address="10.0.0.1",
            port=1000,
            api_port=1001,
            server_ca="ca1",
            api_key="key1",
            core_config_id=None,
        )
        session.add_all([user, node])
        await session.flush()
        user_id, node_id = user.id, node.id
        await session.commit()

    monkeypatch.setattr(
        record_usages.node_manager, "get_healthy_nodes", AsyncMock(return_value=[(node_id, DummyNode(node_id))])
    )

    async def fake_get_users_stats(_: DummyNode, node_id: int | None = None):
        return [{"uid": str(user_id), "value": 150}]

    remove_users = AsyncMock()
    monkeypatch.setattr(record_usages, "get_users_stats", fake_get_users_stats)
    monkeypatch.setattr(record_usages.usage_settings, "disable_recording_node_usage", True)
    monkeypatch.setattr("app.operation.admin_sync.sync_remove_users", remove_users)

    await record_usages.record_user_usages()

    async with session_factory() as session:
        admin_status = await session.execute(select(Admin.status).where(Admin.id == admin_id))
        assert admin_status.scalar_one() == AdminStatus.limited

    remove_users.assert_awaited_once()


@pytest.mark.asyncio
async def test_safe_execute_many_rolls_back_whole_batch_on_error(session_factory):
    async with session_factory() as session:
        admin = Admin(username="admin", hashed_password="secret", role_id=3)
        session.add(admin)
        await session.flush()
        admin_id = admin.id
        user = User(username="user", admin_id=admin_id, proxy_settings=ProxyTable().dict(no_obj=True))
        session.add(user)
        await session.flush()
        user_id = user.id
        await session.commit()

    user_stmt = (
        update(User)
        .where(User.id == bindparam("uid"))
        .values(used_traffic=User.used_traffic + bindparam("value"))
        .execution_options(synchronize_session=False)
    )
    failing_admin_stmt = (
        update(Admin)
        .where(Admin.id == bindparam("admin_id"))
        .values(username=None)
        .execution_options(synchronize_session=False)
    )

    with pytest.raises(DatabaseError):
        await record_usages.safe_execute_many(
            [
                (user_stmt, [{"uid": user_id, "value": 100}]),
                (failing_admin_stmt, [{"admin_id": admin_id}]),
            ]
        )

    async with session_factory() as session:
        used_traffic = await session.execute(select(User.used_traffic).where(User.id == user_id))
        assert used_traffic.scalar_one() == 0


@pytest.mark.asyncio
async def test_calculate_users_usage_rounds_instead_of_truncating():
    users_usage = await record_usages.calculate_users_usage({1: [{"uid": "5", "value": 5}]}, {1: 1.5})
    assert users_usage == [{"uid": 5, "value": 8}]


@pytest.mark.asyncio
async def test_record_user_stats_batched_skips_missing_users(session_factory):
    async with session_factory() as session:
        admin = Admin(username="admin", hashed_password="secret", role_id=3)
        session.add(admin)
        await session.flush()

        user = User(username="user", admin_id=admin.id, proxy_settings=ProxyTable().dict(no_obj=True))
        node = Node(
            name="node-1",
            address="10.0.0.1",
            port=1000,
            api_port=1001,
            server_ca="ca1",
            api_key="key1",
            core_config_id=None,
        )
        session.add_all([user, node])
        await session.flush()
        user_id, node_id = user.id, node.id
        missing_user_id = user_id + 10_000
        await session.commit()

    await record_usages.record_user_stats_batched(
        {
            node_id: [
                {"uid": str(user_id), "value": 100},
                {"uid": str(missing_user_id), "value": 200},
            ]
        },
        {node_id: 1},
    )

    async with session_factory() as session:
        rows = await session.execute(select(NodeUserUsage.user_id, NodeUserUsage.used_traffic))
        records = rows.all()

    assert records == [(user_id, 100)]


@pytest.mark.asyncio
async def test_record_user_stats_batched_chunks_mysql_batches(monkeypatch: pytest.MonkeyPatch):
    executed_param_sizes = []

    async def fake_get_dialect():
        return "mysql"

    async def fake_safe_execute(stmt, params=None, max_retries=5):
        executed_param_sizes.append(len(params))

    monkeypatch.setattr(record_usages, "get_dialect", fake_get_dialect)
    monkeypatch.setattr(record_usages, "safe_execute", fake_safe_execute)
    monkeypatch.setattr(record_usages, "_get_time_bucket", lambda: None)

    params = [{"uid": str(index + 1), "value": 1} for index in range(2_501)]

    await record_usages.record_user_stats_batched({1: params}, {1: 1})

    assert executed_param_sizes == [4_000, 4_000, 2_004]


@pytest.mark.asyncio
async def test_record_user_usages_returns_when_no_usage(monkeypatch: pytest.MonkeyPatch, session_factory):
    async with session_factory() as session:
        admin = Admin(username="admin", hashed_password="secret", role_id=3)
        session.add(admin)
        await session.flush()
        admin_id = admin.id

        user = User(username="user", admin_id=admin_id, proxy_settings=ProxyTable().dict(no_obj=True))
        node = Node(
            name="node-1",
            address="10.0.0.1",
            port=1000,
            api_port=1001,
            server_ca="ca1",
            api_key="key1",
            core_config_id=None,
        )
        session.add_all([user, node])
        await session.flush()
        user_id, node_id = user.id, node.id
        await session.commit()

    nodes = [(node_id, DummyNode(node_id))]
    monkeypatch.setattr(record_usages.node_manager, "get_healthy_nodes", AsyncMock(return_value=nodes))

    async def fake_get_users_stats(_: DummyNode, node_id: int | None = None):
        return []

    monkeypatch.setattr(record_usages, "get_users_stats", fake_get_users_stats)
    monkeypatch.setattr(record_usages.usage_settings, "disable_recording_node_usage", False)

    await record_usages.record_user_usages()

    async with session_factory() as session:
        user_total = await session.execute(select(User.used_traffic).where(User.id == user_id))
        assert user_total.scalar_one() == 0

        admin_total = await session.execute(select(Admin.used_traffic).where(Admin.id == admin_id))
        assert admin_total.scalar_one() == 0

        node_user_usage = await session.execute(select(NodeUserUsage.id))
        assert node_user_usage.first() is None


@pytest.mark.asyncio
async def test_record_node_usages_updates_totals(monkeypatch: pytest.MonkeyPatch, session_factory):
    async with session_factory() as session:
        node_one = Node(
            name="node-1",
            address="10.0.0.1",
            port=1000,
            api_port=1001,
            server_ca="ca1",
            api_key="key1",
            core_config_id=None,
        )
        node_two = Node(
            name="node-2",
            address="10.0.0.2",
            port=1001,
            api_port=1002,
            server_ca="ca2",
            api_key="key2",
            core_config_id=None,
        )
        system = System(uplink=0, downlink=0)
        session.add_all([node_one, node_two, system])
        await session.flush()
        node_one_id, node_two_id, system_id = node_one.id, node_two.id, system.id
        await session.commit()

    nodes = [(node_one_id, DummyNode(node_one_id)), (node_two_id, DummyNode(node_two_id))]
    monkeypatch.setattr(record_usages.node_manager, "get_healthy_nodes", AsyncMock(return_value=nodes))

    stats_map = {
        node_one_id: [{"up": 10, "down": 4}, {"up": 0, "down": 3}],
        node_two_id: [{"up": 1, "down": 1}],
    }

    async def fake_get_outbounds_stats(node: DummyNode, node_id: int | None = None):
        return stats_map[node.node_id]

    monkeypatch.setattr(record_usages, "get_outbounds_stats", fake_get_outbounds_stats)
    monkeypatch.setattr(record_usages.usage_settings, "disable_recording_node_usage", False)

    await record_usages.record_node_usages()

    async with session_factory() as session:
        nodes_result = await session.execute(select(Node.id, Node.uplink, Node.downlink))
        node_totals = {row.id: (row.uplink, row.downlink) for row in nodes_result.all()}
        assert node_totals[node_one_id][0] > node_totals[node_two_id][0]
        assert node_totals[node_two_id][1] > 0

        node_usage_rows = await session.execute(select(NodeUsage.node_id, NodeUsage.uplink, NodeUsage.downlink))
        node_usage_totals = {row.node_id: (row.uplink, row.downlink) for row in node_usage_rows.all()}
        assert set(node_usage_totals.keys()) == {node_one_id, node_two_id}

        assert node_usage_totals[node_one_id][0] >= node_usage_totals[node_two_id][0]
        assert node_usage_totals[node_one_id][1] > 0
        assert node_usage_totals[node_two_id][1] > 0

        system_totals = await session.execute(select(System.uplink, System.downlink).where(System.id == system_id))
        system_row = system_totals.one()
        assert system_row.uplink == sum(values[0] for values in node_totals.values())
        assert system_row.downlink == sum(values[1] for values in node_totals.values())


@pytest.mark.asyncio
async def test_record_node_usages_returns_when_totals_zero(monkeypatch: pytest.MonkeyPatch, session_factory):
    async with session_factory() as session:
        node = Node(
            name="node-1",
            address="10.0.0.1",
            port=1000,
            api_port=1001,
            server_ca="ca1",
            api_key="key1",
            core_config_id=None,
        )
        system = System(uplink=0, downlink=0)
        session.add_all([node, system])
        await session.flush()
        node_id, system_id = node.id, system.id
        await session.commit()

    nodes = [(node_id, DummyNode(node_id))]
    monkeypatch.setattr(record_usages.node_manager, "get_healthy_nodes", AsyncMock(return_value=nodes))

    async def fake_get_outbounds_stats(_: DummyNode, node_id: int | None = None):
        return [{"up": 0, "down": 0}]

    monkeypatch.setattr(record_usages, "get_outbounds_stats", fake_get_outbounds_stats)

    await record_usages.record_node_usages()

    async with session_factory() as session:
        node_row = await session.execute(select(Node.uplink, Node.downlink).where(Node.id == node_id))
        node_totals = node_row.one()
        assert node_totals.uplink == 0
        assert node_totals.downlink == 0

        system_row = await session.execute(select(System.uplink, System.downlink).where(System.id == system_id))
        system_totals = system_row.one()
        assert system_totals.uplink == 0
        assert system_totals.downlink == 0

        node_usage_rows = await session.execute(select(NodeUsage.id))
        assert node_usage_rows.first() is None


class _DeadlockOrig(Exception):
    def __init__(self):
        super().__init__(1213, "Deadlock found when trying to get lock; try restarting transaction")


class _FakeBeginConn:
    def __init__(self, execute):
        self._execute = execute

    async def execute(self, stmt, params=None):
        return await self._execute(stmt, params)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeEngine:
    def __init__(self, execute):
        self._execute = execute

    def execution_options(self, **_kwargs):
        return self

    def begin(self):
        return _FakeBeginConn(self._execute)


@pytest.mark.asyncio
async def test_safe_execute_retries_mysql_deadlock(monkeypatch: pytest.MonkeyPatch):
    attempts = {"n": 0}

    async def flaky_execute(_stmt, _params=None):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise OperationalError("stmt", {}, _DeadlockOrig())

    monkeypatch.setattr(record_usages, "engine", _FakeEngine(flaky_execute))
    monkeypatch.setattr(record_usages, "get_dialect", AsyncMock(return_value="mysql"))
    monkeypatch.setattr(record_usages.asyncio, "sleep", AsyncMock())

    await record_usages.safe_execute("stmt", [{"uid": 1}])

    assert attempts["n"] == 3


@pytest.mark.asyncio
async def test_safe_execute_raises_after_deadlock_retries(monkeypatch: pytest.MonkeyPatch):
    async def always_deadlock(_stmt, _params=None):
        raise OperationalError("stmt", {}, _DeadlockOrig())

    monkeypatch.setattr(record_usages, "engine", _FakeEngine(always_deadlock))
    monkeypatch.setattr(record_usages, "get_dialect", AsyncMock(return_value="mysql"))
    monkeypatch.setattr(record_usages.asyncio, "sleep", AsyncMock())

    with pytest.raises(OperationalError):
        await record_usages.safe_execute("stmt", [{"uid": 1}], max_retries=3)


@pytest.fixture(autouse=True)
def _reset_usage_job_state():
    record_usages._usage_coefficient_cache.clear()
    record_usages._user_usage_running = False
    record_usages._node_usage_running = False
    yield
    record_usages._usage_coefficient_cache.clear()
    record_usages._user_usage_running = False
    record_usages._node_usage_running = False


@pytest.mark.asyncio
async def test_record_user_usages_skips_when_already_running(monkeypatch: pytest.MonkeyPatch, caplog):
    impl = AsyncMock()
    monkeypatch.setattr(record_usages, "_record_user_usages_impl", impl)
    record_usages._user_usage_running = True
    caplog.set_level(logging.WARNING)
    await record_usages.record_user_usages()

    impl.assert_not_awaited()
    assert "JOB_RECORD_USER_USAGES_INTERVAL" in caplog.text
    assert "UVICORN_WORKERS" in caplog.text


@pytest.mark.asyncio
async def test_record_node_usages_skips_when_already_running(monkeypatch: pytest.MonkeyPatch, caplog):
    impl = AsyncMock()
    monkeypatch.setattr(record_usages, "_record_node_usages_impl", impl)
    record_usages._node_usage_running = True
    caplog.set_level(logging.WARNING)
    await record_usages.record_node_usages()

    impl.assert_not_awaited()
    assert "JOB_RECORD_NODE_USAGES_INTERVAL" in caplog.text
    assert "UVICORN_WORKERS" in caplog.text


@pytest.mark.asyncio
async def test_record_user_usages_does_not_apply_global_timeout(monkeypatch: pytest.MonkeyPatch):
    impl = AsyncMock()
    wait_for = AsyncMock(side_effect=AssertionError("wait_for should not run"))
    monkeypatch.setattr(record_usages, "_record_user_usages_impl", impl)
    monkeypatch.setattr(record_usages.asyncio, "wait_for", wait_for)

    await record_usages.record_user_usages()

    impl.assert_awaited_once()
    wait_for.assert_not_awaited()


@pytest.mark.asyncio
async def test_record_user_usages_warns_when_slower_than_interval(monkeypatch: pytest.MonkeyPatch, caplog):
    clock = {"t": 0.0}

    async def impl():
        clock["t"] = 15.0

    monkeypatch.setattr(record_usages.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(record_usages, "_record_user_usages_impl", impl)
    monkeypatch.setattr(record_usages.job_settings, "record_user_usages_interval", 10)
    caplog.set_level(logging.WARNING)

    await record_usages.record_user_usages()

    assert "exceeds the 10s interval" in caplog.text
    assert "UVICORN_WORKERS" in caplog.text


@pytest.mark.asyncio
async def test_usage_coefficient_is_cached_across_collects(monkeypatch: pytest.MonkeyPatch):
    node = DummyNode(1, usage_coefficient=2)
    extra_calls = {"n": 0}
    original_get_extra = node.get_extra

    async def counting_get_extra():
        extra_calls["n"] += 1
        return await original_get_extra()

    node.get_extra = counting_get_extra
    monkeypatch.setattr(record_usages, "get_users_stats", AsyncMock(return_value=[]))

    first = await record_usages._collect_node_user_usage(node, 1)
    second = await record_usages._collect_node_user_usage(node, 1)

    assert extra_calls["n"] == 1
    assert first[1] == second[1] == 2.0


async def _seed_billing_fixture(
    session_factory,
    *,
    node_coefficients: list[float],
    user_names: list[str],
    admin_names: list[str] | None = None,
):
    admin_names = admin_names or ["billing-admin"]
    async with session_factory() as session:
        admins = [Admin(username=name, hashed_password="secret", role_id=3) for name in admin_names]
        session.add_all(admins)
        await session.flush()
        admin_ids = [admin.id for admin in admins]

        users = [
            User(
                username=name,
                admin_id=admin_ids[index % len(admin_ids)],
                proxy_settings=ProxyTable().dict(no_obj=True),
            )
            for index, name in enumerate(user_names)
        ]
        session.add_all(users)
        await session.flush()
        user_ids = [user.id for user in users]

        nodes = [
            Node(
                name=f"{user_names[0]}-node-{index}",
                address=f"10.9.{index}.1",
                port=9000 + index * 2,
                api_port=9001 + index * 2,
                server_ca=f"ca{index}",
                api_key=f"key{index}",
                core_config_id=None,
            )
            for index in range(len(node_coefficients))
        ]
        session.add_all(nodes)
        await session.flush()
        node_ids = [node.id for node in nodes]
        await session.commit()

    return admin_ids, user_ids, node_ids


def _install_nodes(monkeypatch: pytest.MonkeyPatch, node_ids, coefficients, stats_map):
    nodes = [
        (node_id, DummyNode(node_id, usage_coefficient=coeff)) for node_id, coeff in zip(node_ids, coefficients)
    ]
    monkeypatch.setattr(record_usages.node_manager, "get_healthy_nodes", AsyncMock(return_value=nodes))

    async def fake_get_users_stats(node: DummyNode, node_id: int | None = None):
        return stats_map[node.node_id]

    monkeypatch.setattr(record_usages, "get_users_stats", fake_get_users_stats)
    monkeypatch.setattr(record_usages.usage_settings, "disable_recording_node_usage", False)
    return nodes


async def _read_totals(session_factory, user_ids, admin_ids):
    async with session_factory() as session:
        user_rows = (await session.execute(select(User.id, User.used_traffic).where(User.id.in_(user_ids)))).all()
        admin_rows = (await session.execute(select(Admin.id, Admin.used_traffic).where(Admin.id.in_(admin_ids)))).all()
        node_rows = (
            await session.execute(select(NodeUserUsage.user_id, NodeUserUsage.node_id, NodeUserUsage.used_traffic))
        ).all()
    return (
        {row.id: row.used_traffic for row in user_rows},
        {row.id: row.used_traffic for row in admin_rows},
        node_rows,
    )


def test_usage_jobs_cannot_overlap_themselves():
    if not runtime_settings.role.runs_node:
        pytest.skip("usage jobs are only registered on node-running roles")
    for job_id in ("record_user_usages", "record_node_usages"):
        job = record_usages.scheduler.get_job(job_id)
        assert job is not None, job_id
        assert job.max_instances == 1, job_id
        assert job.coalesce is True, job_id


@pytest.mark.asyncio
async def test_usage_writes_are_ordered_by_user_id(monkeypatch: pytest.MonkeyPatch, session_factory):
    admin_ids, user_ids, node_ids = await _seed_billing_fixture(
        session_factory,
        node_coefficients=[1.0, 1.0],
        user_names=[f"ordered-user-{index}" for index in range(4)],
        admin_names=["ordered-admin-1", "ordered-admin-2"],
    )

    reversed_ids = list(reversed(user_ids))
    stats_map = {node_id: [{"uid": str(uid), "value": 10} for uid in reversed_ids] for node_id in node_ids}
    _install_nodes(monkeypatch, list(reversed(node_ids)), [1.0, 1.0], stats_map)

    captured: dict[str, Any] = {"users": None, "admins": None, "node_batches": []}
    real_builder = record_usages.build_node_user_usage_upsert

    def capturing_builder(dialect, batch):
        captured["node_batches"].append([(item["uid"], item["node_id"]) for item in batch])
        return real_builder(dialect, batch)

    async def capturing_execute_many(statements, max_retries=record_usages.DEADLOCK_MAX_RETRIES):
        for stmt, params in statements:
            table_name = getattr(getattr(stmt, "table", None), "name", "")
            if table_name == "users":
                captured["users"] = [item["uid"] for item in params]
            elif table_name == "admins":
                captured["admins"] = [item["admin_id"] for item in params]

    monkeypatch.setattr(record_usages, "build_node_user_usage_upsert", capturing_builder)
    monkeypatch.setattr(record_usages, "safe_execute_many", capturing_execute_many)

    await record_usages.record_user_usages()

    assert captured["users"] == sorted(user_ids)
    assert captured["admins"] == sorted(admin_ids)
    assert captured["node_batches"]
    for batch in captured["node_batches"]:
        assert batch == sorted(batch)


@pytest.mark.asyncio
async def test_failed_node_usage_write_leaves_no_billing(monkeypatch: pytest.MonkeyPatch, session_factory):
    admin_ids, user_ids, node_ids = await _seed_billing_fixture(
        session_factory,
        node_coefficients=[1.0],
        user_names=["evidence-user"],
        admin_names=["evidence-admin"],
    )
    stats_map = {node_ids[0]: [{"uid": str(user_ids[0]), "value": 500}]}
    _install_nodes(monkeypatch, node_ids, [1.0], stats_map)

    async def exploding_record_user_stats_batched(all_node_params, usage_coefficients):
        raise RuntimeError("node_user_usages write failed")

    monkeypatch.setattr(record_usages, "record_user_stats_batched", exploding_record_user_stats_batched)

    with pytest.raises(RuntimeError):
        await record_usages.record_user_usages()

    user_totals, admin_totals, node_rows = await _read_totals(session_factory, user_ids, admin_ids)
    assert user_totals[user_ids[0]] == 0
    assert admin_totals[admin_ids[0]] == 0
    assert node_rows == []


@pytest.mark.asyncio
async def test_zero_usage_coefficient_is_not_coerced_to_one():
    node = DummyNode(4242, usage_coefficient=0)
    assert await record_usages._node_usage_coefficient(node, 4242) == 0.0


@pytest.mark.asyncio
async def test_zero_usage_coefficient_node_bills_nothing(monkeypatch: pytest.MonkeyPatch, session_factory):
    admin_ids, user_ids, node_ids = await _seed_billing_fixture(
        session_factory,
        node_coefficients=[0.0],
        user_names=["free-user"],
        admin_names=["free-admin"],
    )
    stats_map = {node_ids[0]: [{"uid": str(user_ids[0]), "value": 1000}]}
    _install_nodes(monkeypatch, node_ids, [0.0], stats_map)

    await record_usages.record_user_usages()

    user_totals, admin_totals, node_rows = await _read_totals(session_factory, user_ids, admin_ids)
    assert user_totals[user_ids[0]] == 0
    assert admin_totals[admin_ids[0]] == 0
    assert all(row.used_traffic == 0 for row in node_rows)


@pytest.mark.asyncio
async def test_fractional_usage_coefficient_is_applied(monkeypatch: pytest.MonkeyPatch, session_factory):
    admin_ids, user_ids, node_ids = await _seed_billing_fixture(
        session_factory,
        node_coefficients=[2.5],
        user_names=["fractional-user"],
        admin_names=["fractional-admin"],
    )
    stats_map = {node_ids[0]: [{"uid": str(user_ids[0]), "value": 100}]}
    _install_nodes(monkeypatch, node_ids, [2.5], stats_map)

    await record_usages.record_user_usages()

    user_totals, admin_totals, node_rows = await _read_totals(session_factory, user_ids, admin_ids)
    assert user_totals[user_ids[0]] == 250
    assert admin_totals[admin_ids[0]] == 250
    assert sum(row.used_traffic for row in node_rows) == 250


@pytest.mark.asyncio
async def test_user_node_and_admin_totals_stay_consistent(monkeypatch: pytest.MonkeyPatch, session_factory):
    admin_ids, user_ids, node_ids = await _seed_billing_fixture(
        session_factory,
        node_coefficients=[2.5, 0.5],
        user_names=["totals-user-1", "totals-user-2"],
        admin_names=["totals-admin"],
    )
    stats_map = {
        node_ids[0]: [{"uid": str(user_ids[0]), "value": 100}, {"uid": str(user_ids[1]), "value": 40}],
        node_ids[1]: [{"uid": str(user_ids[0]), "value": 50}],
    }
    _install_nodes(monkeypatch, node_ids, [2.5, 0.5], stats_map)

    await record_usages.record_user_usages()

    user_totals, admin_totals, node_rows = await _read_totals(session_factory, user_ids, admin_ids)
    per_user_node_totals: dict[int, int] = defaultdict(int)
    for row in node_rows:
        per_user_node_totals[row.user_id] += row.used_traffic

    assert user_totals[user_ids[0]] == 275
    assert user_totals[user_ids[1]] == 100
    assert dict(per_user_node_totals) == user_totals
    assert admin_totals[admin_ids[0]] == sum(user_totals.values())


@pytest.mark.asyncio
async def test_reset_during_collection_is_not_billed(monkeypatch: pytest.MonkeyPatch, session_factory):
    admin_ids, user_ids, node_ids = await _seed_billing_fixture(
        session_factory,
        node_coefficients=[1.0],
        user_names=["reset-user", "kept-user"],
        admin_names=["reset-admin"],
    )
    reset_user_id, kept_user_id = user_ids

    nodes = [(node_ids[0], DummyNode(node_ids[0]))]
    monkeypatch.setattr(record_usages.node_manager, "get_healthy_nodes", AsyncMock(return_value=nodes))
    monkeypatch.setattr(record_usages.usage_settings, "disable_recording_node_usage", False)

    async def racing_get_users_stats(node: DummyNode, node_id: int | None = None):
        async with usage_apply_barrier.reset(None, [reset_user_id]):
            pass
        return [{"uid": str(reset_user_id), "value": 900}, {"uid": str(kept_user_id), "value": 70}]

    monkeypatch.setattr(record_usages, "get_users_stats", racing_get_users_stats)

    await record_usages.record_user_usages()

    user_totals, admin_totals, node_rows = await _read_totals(session_factory, user_ids, admin_ids)
    assert user_totals[reset_user_id] == 0
    assert user_totals[kept_user_id] == 70
    assert admin_totals[admin_ids[0]] == 70
    assert {row.user_id for row in node_rows} == {kept_user_id}


@pytest.mark.asyncio
async def test_reset_committed_elsewhere_during_collection_is_not_billed(
    monkeypatch: pytest.MonkeyPatch, session_factory
):
    admin_ids, user_ids, node_ids = await _seed_billing_fixture(
        session_factory,
        node_coefficients=[1.0],
        user_names=["log-reset-user", "log-kept-user"],
        admin_names=["log-reset-admin"],
    )
    reset_user_id, kept_user_id = user_ids

    nodes = [(node_ids[0], DummyNode(node_ids[0]))]
    monkeypatch.setattr(record_usages.node_manager, "get_healthy_nodes", AsyncMock(return_value=nodes))
    monkeypatch.setattr(record_usages.usage_settings, "disable_recording_node_usage", False)

    async def racing_get_users_stats(node: DummyNode, node_id: int | None = None):
        async with session_factory() as session:
            session.add(UserUsageResetLogs(user_id=reset_user_id, used_traffic_at_reset=123))
            await session.commit()
        return [{"uid": str(reset_user_id), "value": 900}, {"uid": str(kept_user_id), "value": 70}]

    monkeypatch.setattr(record_usages, "get_users_stats", racing_get_users_stats)

    await record_usages.record_user_usages()

    user_totals, admin_totals, node_rows = await _read_totals(session_factory, user_ids, admin_ids)
    assert user_totals[reset_user_id] == 0
    assert user_totals[kept_user_id] == 70
    assert admin_totals[admin_ids[0]] == 70
    assert {row.user_id for row in node_rows} == {kept_user_id}


@pytest.mark.asyncio
async def test_usage_apply_barrier_serializes_reset_against_apply():
    barrier = UsageApplyBarrier()
    order: list[str] = []

    async def apply_side():
        async with barrier.window() as window:
            await asyncio.sleep(0)
            async with barrier.apply(window) as marked:
                order.append("apply-start")
                assert marked == frozenset()
                await asyncio.sleep(0.05)
                order.append("apply-end")

    async def reset_side():
        await asyncio.sleep(0.01)
        async with barrier.reset(None, [1]):
            order.append("reset")

    await asyncio.gather(apply_side(), reset_side())

    assert order == ["apply-start", "apply-end", "reset"]
    assert barrier.held_user_ids == frozenset()


@pytest.mark.asyncio
async def test_usage_apply_barrier_marks_open_windows():
    barrier = UsageApplyBarrier()
    async with barrier.window() as window:
        async with barrier.reset(None, [7, 9, None]):
            pass
        async with barrier.apply(window) as marked:
            assert marked == frozenset({7, 9})
    assert barrier.open_windows == 0


@pytest.mark.asyncio
async def test_usage_apply_barrier_hold_covers_windows_opened_before_commit(session_factory):
    barrier = UsageApplyBarrier()
    async with session_factory() as session:
        async with barrier.reset(session, [11]):
            await session.execute(select(User.id))

        assert barrier.held_user_ids == frozenset({11})

        async with barrier.window() as window, barrier.apply(window) as marked:
            assert marked == frozenset({11})

        await session.rollback()

    assert barrier.held_user_ids == frozenset()


async def _commit_external_usage(session_factory, user_id: int, amount: int) -> None:
    async with session_factory() as session:
        await session.execute(
            update(User).where(User.id == user_id).values(used_traffic=User.used_traffic + amount)
        )
        await session.commit()


async def _read_reset_logs(session_factory, user_ids) -> dict[int, list[int]]:
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(UserUsageResetLogs.user_id, UserUsageResetLogs.used_traffic_at_reset).where(
                    UserUsageResetLogs.user_id.in_(user_ids)
                )
            )
        ).all()
    logs: dict[int, list[int]] = defaultdict(list)
    for row in rows:
        logs[row.user_id].append(row.used_traffic_at_reset)
    return dict(logs)


@pytest.mark.asyncio
async def test_reset_logs_and_clears_traffic_committed_outside_the_session(session_factory):
    _, user_ids, _ = await _seed_billing_fixture(
        session_factory,
        node_coefficients=[],
        user_names=["stale-reset-user"],
        admin_names=["stale-reset-admin"],
    )
    user_id = user_ids[0]

    async with session_factory() as session:
        db_user = (await session.execute(select(User).where(User.id == user_id))).scalar_one()
        await session.commit()
        assert db_user.used_traffic == 0

        await _commit_external_usage(session_factory, user_id, 500)

        await reset_user_data_usage(session, db_user)

    assert await _read_reset_logs(session_factory, [user_id]) == {user_id: [500]}
    user_totals, _, _ = await _read_totals(session_factory, [user_id], [])
    assert user_totals[user_id] == 0


@pytest.mark.asyncio
async def test_bulk_reset_logs_committed_used_traffic(session_factory):
    _, user_ids, _ = await _seed_billing_fixture(
        session_factory,
        node_coefficients=[],
        user_names=["bulk-reset-user-1", "bulk-reset-user-2"],
        admin_names=["bulk-reset-admin"],
    )

    async with session_factory() as session:
        db_users = list(
            (await session.execute(select(User).where(User.id.in_(user_ids)).order_by(User.id))).scalars().all()
        )
        await session.commit()

        await _commit_external_usage(session_factory, user_ids[0], 700)
        await _commit_external_usage(session_factory, user_ids[1], 300)

        await bulk_reset_user_data_usage(session, db_users)

    assert await _read_reset_logs(session_factory, user_ids) == {user_ids[0]: [700], user_ids[1]: [300]}
    user_totals, _, _ = await _read_totals(session_factory, user_ids, [])
    assert user_totals == {user_ids[0]: 0, user_ids[1]: 0}


@pytest.mark.asyncio
async def test_reset_all_users_data_usage_deletes_reset_history(session_factory):
    _, user_ids, _ = await _seed_billing_fixture(
        session_factory,
        node_coefficients=[],
        user_names=["wipe-user"],
        admin_names=["wipe-admin"],
    )
    user_id = user_ids[0]

    async with session_factory() as session:
        session.add(UserUsageResetLogs(user_id=user_id, used_traffic_at_reset=4096))
        await session.commit()

    await _commit_external_usage(session_factory, user_id, 900)

    async with session_factory() as session:
        await reset_all_users_data_usage(session)

    assert await _read_reset_logs(session_factory, [user_id]) == {}
    user_totals, _, _ = await _read_totals(session_factory, [user_id], [])
    assert user_totals[user_id] == 0


@pytest.mark.asyncio
async def test_crud_reset_marks_an_open_usage_window(session_factory):
    _, user_ids, _ = await _seed_billing_fixture(
        session_factory,
        node_coefficients=[],
        user_names=["window-reset-user"],
        admin_names=["window-reset-admin"],
    )
    user_id = user_ids[0]

    async with usage_apply_barrier.window() as window:
        async with session_factory() as session:
            db_user = (await session.execute(select(User).where(User.id == user_id))).scalar_one()
            await reset_user_data_usage(session, db_user)

        async with usage_apply_barrier.apply(window) as marked:
            assert user_id in marked

    assert usage_apply_barrier.held_user_ids == frozenset()


def test_scheduler_defaults_serialize_unflagged_jobs():
    defaults = record_usages.scheduler._job_defaults
    assert defaults["max_instances"] == 1
    assert defaults["coalesce"] is True


@pytest.mark.asyncio
async def test_reset_body_does_not_hold_the_barrier_lock():
    barrier = UsageApplyBarrier()
    order: list[str] = []
    reset_marked = asyncio.Event()

    async def slow_reset():
        async with barrier.reset(None, [21]):
            order.append("reset-marked")
            reset_marked.set()
            await asyncio.sleep(0.05)
            order.append("reset-committed")

    async def apply_side():
        await reset_marked.wait()
        async with barrier.window() as window, barrier.apply(window) as marked:
            order.append("apply")
            assert marked == frozenset({21})

    await asyncio.gather(slow_reset(), apply_side())

    assert order == ["reset-marked", "apply", "reset-committed"]
    assert barrier.held_user_ids == frozenset()


@pytest.mark.asyncio
async def test_apply_blocks_a_reset_from_marking_midway():
    barrier = UsageApplyBarrier()
    order: list[str] = []
    apply_started = asyncio.Event()

    async def apply_side():
        async with barrier.window() as window, barrier.apply(window) as marked:
            apply_started.set()
            assert marked == frozenset()
            await asyncio.sleep(0.05)
            order.append("apply-committed")

    async def reset_side():
        await apply_started.wait()
        async with barrier.reset(None, [33]):
            order.append("reset-marked")

    await asyncio.gather(apply_side(), reset_side())

    assert order == ["apply-committed", "reset-marked"]
