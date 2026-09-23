import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.crud.node import remove_node
from app.db.models import Admin, AdminRole, Base, Node, NodeUserUsage, User
from app.jobs import record_usages
from app.models.proxy import ProxyTable

BUCKET = datetime(2026, 9, 23, 10, 0, tzinfo=UTC)


def _enforce_foreign_keys(dbapi_conn, record):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


@pytest.fixture
async def fk_engine(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fk.sqlite3'}")
    event.listen(engine.sync_engine, "connect", _enforce_foreign_keys)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async def _dialect():
        return "sqlite"

    monkeypatch.setattr(record_usages, "engine", engine)
    monkeypatch.setattr(record_usages, "get_dialect", _dialect)
    yield engine
    await engine.dispose()


def _node(name):
    return Node(
        name=name,
        address="127.0.0.1",
        port=62050,
        api_port=62051,
        server_ca="ca",
        api_key=str(uuid4()),
        core_config_id=None,
    )


@pytest.mark.asyncio
async def test_deleting_one_node_mid_tick_does_not_void_the_cohorts_billing(fk_engine):
    session_factory = async_sessionmaker(fk_engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        role = AdminRole(name=f"role-{uuid4().hex[:6]}")
        db.add(role)
        await db.commit()
        admin = Admin(username=f"owner-{uuid4().hex[:6]}", hashed_password="x", role_id=role.id)
        db.add(admin)
        kept, doomed = _node("kept"), _node("doomed")
        db.add_all([kept, doomed])
        await db.commit()
        alice = User(username="alice", proxy_settings=ProxyTable().dict(), admin_id=admin.id)
        bob = User(username="bob", proxy_settings=ProxyTable().dict(), admin_id=admin.id)
        db.add_all([alice, bob])
        await db.commit()
        ids = (alice.id, bob.id, admin.id, kept.id, doomed.id)

    alice_id, bob_id, admin_id, kept_id, doomed_id = ids

    async with session_factory() as db:
        await remove_node(db, await db.get(Node, doomed_id))

    failure = None
    try:
        await record_usages.apply_fenced_user_usage(
            [{"uid": alice_id, "value": 700}, {"uid": bob_id, "value": 300}],
            {alice_id: record_usages.UserUsageContext(admin_id, 0), bob_id: record_usages.UserUsageContext(admin_id, 0)},
            {alice_id: 0, bob_id: 0},
            [
                {"uid": alice_id, "node_id": kept_id, "created_at": BUCKET, "value": 700},
                {"uid": bob_id, "node_id": doomed_id, "created_at": BUCKET, "value": 300},
            ],
        )
    except Exception as exc:
        failure = type(exc).__name__

    async with session_factory() as db:
        billed = dict((await db.execute(select(User.username, User.used_traffic))).all())

    assert (failure, billed) == (None, {"alice": 700, "bob": 300})


@pytest.mark.asyncio
async def test_usage_on_a_node_deleted_mid_tick_is_charted_without_a_node_so_the_invariant_holds(fk_engine):
    session_factory = async_sessionmaker(fk_engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        role = AdminRole(name=f"role-{uuid4().hex[:6]}")
        db.add(role)
        await db.commit()
        admin = Admin(username=f"owner-{uuid4().hex[:6]}", hashed_password="x", role_id=role.id)
        db.add(admin)
        kept, doomed = _node("kept"), _node("doomed")
        db.add_all([kept, doomed])
        await db.commit()
        carol = User(username="carol", proxy_settings=ProxyTable().dict(), admin_id=admin.id)
        db.add(carol)
        await db.commit()
        carol_id, admin_id, kept_id, doomed_id = carol.id, admin.id, kept.id, doomed.id

    async with session_factory() as db:
        await remove_node(db, await db.get(Node, doomed_id))

    await record_usages.apply_fenced_user_usage(
        [{"uid": carol_id, "value": 1_000}],
        {carol_id: record_usages.UserUsageContext(admin_id, 0)},
        {carol_id: 0},
        [
            {"uid": carol_id, "node_id": kept_id, "created_at": BUCKET, "value": 600},
            {"uid": carol_id, "node_id": doomed_id, "created_at": BUCKET, "value": 400},
        ],
    )

    async with session_factory() as db:
        used = (await db.execute(select(User.used_traffic).where(User.id == carol_id))).scalar_one()
        credited = (await db.execute(select(Admin.used_traffic).where(Admin.id == admin_id))).scalar_one()
        charted = dict(
            (
                await db.execute(
                    select(NodeUserUsage.node_id, func.sum(NodeUserUsage.used_traffic))
                    .where(NodeUserUsage.user_id == carol_id)
                    .group_by(NodeUserUsage.node_id)
                )
            ).all()
        )
    assert (used, credited) == (1_000, 1_000)
    assert charted == {kept_id: 600, None: 400}
    assert used - sum(charted.values()) == 0


@pytest.mark.asyncio
async def test_a_node_deleted_between_the_chart_read_and_its_foreign_key_check_does_not_void_billing(fk_engine):
    session_factory = async_sessionmaker(fk_engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        role = AdminRole(name=f"role-{uuid4().hex[:6]}")
        db.add(role)
        await db.commit()
        admin = Admin(username=f"owner-{uuid4().hex[:6]}", hashed_password="x", role_id=role.id)
        db.add(admin)
        node = _node("racing")
        db.add(node)
        await db.commit()
        dave = User(username="dave", proxy_settings=ProxyTable().dict(), admin_id=admin.id)
        db.add(dave)
        await db.commit()
        dave_id, admin_id, node_id = dave.id, admin.id, node.id

    raised = {"count": 0}

    def fk_fails_once(conn, cursor, statement, parameters, context, executemany):
        if raised["count"] == 0 and " ".join(statement.upper().split()).startswith("INSERT INTO NODE_USER_USAGES"):
            raised["count"] += 1
            raise IntegrityError(statement, parameters, sqlite3.IntegrityError("FOREIGN KEY constraint failed"))

    event.listen(fk_engine.sync_engine, "before_cursor_execute", fk_fails_once)
    try:
        await record_usages.apply_fenced_user_usage(
            [{"uid": dave_id, "value": 900}],
            {dave_id: record_usages.UserUsageContext(admin_id, 0)},
            {dave_id: 0},
            [{"uid": dave_id, "node_id": node_id, "created_at": BUCKET, "value": 900}],
        )
    finally:
        event.remove(fk_engine.sync_engine, "before_cursor_execute", fk_fails_once)

    async with session_factory() as db:
        used = (await db.execute(select(User.used_traffic).where(User.id == dave_id))).scalar_one()
        charted = (
            await db.execute(select(func.sum(NodeUserUsage.used_traffic)).where(NodeUserUsage.user_id == dave_id))
        ).scalar_one()
    assert raised["count"] == 1
    assert (used, charted) == (900, 900)
