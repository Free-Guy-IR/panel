from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from app.db import base
from app.db.models import Admin, AdminRole, Node, NodeUserUsage, User
from app.jobs import record_usages
from app.models.proxy import ProxyTable
from app.operation import admin_sync
from config import database_settings


def _database_url() -> str:
    if os.getenv("TEST_FROM", "local").lower() == "local":
        return "sqlite+aiosqlite:///:memory:"
    return database_settings.url


async def _set_sqlite_foreign_keys(engine, enabled: bool) -> None:
    async with engine.connect() as conn:
        await conn.exec_driver_sql(f"PRAGMA foreign_keys={'ON' if enabled else 'OFF'}")
        await conn.commit()


@pytest.fixture(name="recorder_db")
async def recorder_db_fixture(monkeypatch: pytest.MonkeyPatch):
    url = _database_url()
    is_sqlite = url.startswith("sqlite")
    json_column = base.Base.metadata.tables["users"].c.proxy_settings
    json_default = json_column.server_default
    if url.startswith("mysql"):
        json_column.server_default = None

    if is_sqlite:
        engine = create_async_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    else:
        engine = create_async_engine(url, poolclass=NullPool)

    async with engine.begin() as conn:
        await conn.run_sync(base.Base.metadata.drop_all)
        await conn.run_sync(base.Base.metadata.create_all)
    if is_sqlite:
        await _set_sqlite_foreign_keys(engine, True)
        async with engine.connect() as conn:
            assert (await conn.execute(text("PRAGMA foreign_keys"))).scalar_one() == 1

    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    async with session_factory() as session:
        session.add_all(
            [
                AdminRole(name="owner", is_owner=True, permissions={}, limits={}, features={}, access={}),
                AdminRole(name="administrator", is_owner=False, permissions={}, limits={}, features={}, access={}),
                AdminRole(name="operator", is_owner=False, permissions={}, limits={}, features={}, access={}),
            ]
        )
        await session.commit()

    class RecorderGetDB:
        def __init__(self):
            self.db = session_factory()

        async def __aenter__(self):
            return self.db

        async def __aexit__(self, exc_type, exc_value, traceback):
            if isinstance(exc_value, SQLAlchemyError):
                await self.db.rollback()
            await self.db.close()

    monkeypatch.setattr(record_usages, "engine", engine)
    monkeypatch.setattr(record_usages, "GetDB", RecorderGetDB)
    monkeypatch.setattr(admin_sync, "GetDB", RecorderGetDB)
    monkeypatch.setattr(record_usages, "_retained_cohorts", [], raising=False)
    monkeypatch.setattr(record_usages, "_usage_job_runs", set(), raising=False)
    monkeypatch.setattr(record_usages, "_user_usage_running", False)
    monkeypatch.setattr(record_usages, "_node_usage_running", False)
    monkeypatch.setattr(record_usages.usage_settings, "disable_recording_node_usage", False)
    record_usages._usage_coefficient_cache.clear()

    yield session_factory

    record_usages._usage_coefficient_cache.clear()
    if is_sqlite:
        await _set_sqlite_foreign_keys(engine, False)
    async with engine.begin() as conn:
        await conn.run_sync(base.Base.metadata.drop_all)
        await conn.run_sync(base.Base.metadata.create_all)
    await engine.dispose()
    json_column.server_default = json_default


async def seed_users_and_nodes(session_factory, user_count: int, node_count: int) -> tuple[int, list[int], list[int]]:
    tag = uuid4().hex[:10]
    async with session_factory() as session:
        admin = Admin(username=f"recorder-admin-{tag}", hashed_password="secret", role_id=3)
        session.add(admin)
        await session.flush()
        users = [
            User(
                username=f"recorder-user-{tag}-{index}",
                admin_id=admin.id,
                proxy_settings=ProxyTable().dict(no_obj=True),
            )
            for index in range(user_count)
        ]
        nodes = [
            Node(
                name=f"recorder-node-{tag}-{index}",
                address=f"10.77.{index}.1",
                port=7000 + index * 2,
                api_port=7001 + index * 2,
                server_ca=f"ca-{index}",
                api_key=f"key-{tag}-{index}",
                core_config_id=None,
            )
            for index in range(node_count)
        ]
        session.add_all([*users, *nodes])
        await session.flush()
        admin_id = admin.id
        user_ids = [user.id for user in users]
        node_ids = [node.id for node in nodes]
        await session.commit()
    return admin_id, user_ids, node_ids


class CountingNode:
    def __init__(self, node_id: int, counters: dict[int, int], latency: float = 0.0, coefficient: float = 1.0):
        self.node_id = node_id
        self.counters = dict(counters)
        self.latency = latency
        self.coefficient = coefficient
        self.reads = 0
        self.cancelled_reads = 0
        self.read_issued = asyncio.Event()

    async def get_extra(self) -> dict:
        return {"usage_coefficient": self.coefficient}

    async def get_stats(self, stat_type, reset: bool, timeout: int):
        self.reads += 1
        snapshot = dict(self.counters)
        if reset:
            self.counters = {uid: 0 for uid in snapshot}
        self.read_issued.set()
        try:
            await asyncio.sleep(self.latency)
        except asyncio.CancelledError:
            self.cancelled_reads += 1
            raise
        return SimpleNamespace(
            stats=[SimpleNamespace(name=str(uid), value=value) for uid, value in snapshot.items() if value]
        )

    def add(self, uid: int, value: int) -> None:
        self.counters[uid] = self.counters.get(uid, 0) + value


async def usage_snapshot(session_factory, user_ids: list[int]) -> tuple[dict[int, int], dict[int, int]]:
    async with session_factory() as session:
        billed = {
            row.id: int(row.used_traffic)
            for row in (await session.execute(select(User.id, User.used_traffic).where(User.id.in_(user_ids)))).all()
        }
        chart_rows = (
            await session.execute(
                select(NodeUserUsage.user_id, func.sum(NodeUserUsage.used_traffic))
                .where(NodeUserUsage.user_id.in_(user_ids))
                .group_by(NodeUserUsage.user_id)
            )
        ).all()
    charted: dict[int, int] = defaultdict(int)
    for user_id, total in chart_rows:
        charted[int(user_id)] = int(total or 0)
    return billed, {user_id: charted[user_id] for user_id in user_ids}
