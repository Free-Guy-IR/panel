import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db import base
from app.db.crud.user import _USER_DEPENDENT_MODELS, remove_user, remove_users
from app.db.models import (
    NextPlan,
    NodeUserUsage,
    NotificationReminder,
    ReminderType,
    User,
    UserHWID,
    UserSubscriptionAccess,
    UserSubscriptionUpdate,
    UserUsageResetLogs,
)
from app.fork.models.connection import ConnectionRestriction, UserConnectionLimit, UserConnectionState
from app.fork.models.pending_user_resync import PendingUserResync
from app.fork.models.proxy_secret_claim import ProxySecretClaim
from app.jobs.record_usages import build_node_user_usage_upsert

CASCADED_MODELS = (
    NodeUserUsage,
    UserUsageResetLogs,
    UserSubscriptionUpdate,
    UserSubscriptionAccess,
    UserHWID,
    NextPlan,
    NotificationReminder,
    UserConnectionLimit,
    ConnectionRestriction,
    UserConnectionState,
    PendingUserResync,
)
USAGE_HOUR = datetime(2026, 1, 1, tzinfo=UTC)


async def _engine(foreign_keys: bool, url: str = "sqlite+aiosqlite:///:memory:"):
    options = {"connect_args": {"check_same_thread": False}}
    if url.endswith(":memory:"):
        options["poolclass"] = StaticPool
    engine = create_async_engine(url, **options)

    @event.listens_for(engine.sync_engine, "connect")
    def _pragmas(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys else 'OFF'}")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(base.Base.metadata.create_all)
    return engine


@pytest.fixture(params=[False, True], ids=["fk-off", "fk-on"])
async def sqlite_db(request):
    engine = await _engine(request.param)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _user_with_every_dependent(db, username: str) -> User:
    user = User(username=username, proxy_settings={})
    db.add(user)
    await db.flush()
    db.add_all(
        [
            NodeUserUsage(created_at=USAGE_HOUR, user_id=user.id, node_id=None, used_traffic=10),
            UserUsageResetLogs(user_id=user.id, used_traffic_at_reset=1024),
            UserSubscriptionUpdate(user_id=user.id, user_agent="client/1.0"),
            UserSubscriptionAccess(user_id=user.id, access_kind="subscription"),
            UserHWID(user_id=user.id, hwid=f"hwid-{username}"),
            NextPlan(user_id=user.id, user_template_id=None, data_limit=5),
            NotificationReminder(user_id=user.id, type=ReminderType.data_usage, threshold=80),
            UserConnectionLimit(user_id=user.id, ip_limit=2),
            ConnectionRestriction(user_id=user.id),
            UserConnectionState(user_id=user.id),
            PendingUserResync(user_id=user.id, token="t" * 32, requested_at=USAGE_HOUR),
            ProxySecretClaim(kind="vless", digest="d" * 64, user_id=user.id, claimed_at=USAGE_HOUR),
        ]
    )
    await db.commit()
    return user


async def _rows_owned_by(db, user_id: int) -> dict[str, int]:
    counts = {}
    for model in (*CASCADED_MODELS, ProxySecretClaim):
        counts[model.__tablename__] = await db.scalar(
            select(func.count()).select_from(model).where(model.user_id == user_id)
        )
    return counts


def test_every_cascaded_user_table_is_cleaned_on_sqlite():
    assert set(_USER_DEPENDENT_MODELS) == set(CASCADED_MODELS)
    cascaded = set()
    for table in base.Base.metadata.sorted_tables:
        for fk in table.foreign_keys:
            if (
                fk.column.table.name == "users"
                and fk.ondelete == "CASCADE"
                and table.name != "users_groups_association"
            ):
                cascaded.add(table.name)
    assert cascaded == {model.__tablename__ for model in CASCADED_MODELS}


@pytest.mark.asyncio
@pytest.mark.parametrize("bulk", [False, True], ids=["single", "bulk"])
async def test_reused_user_id_inherits_no_fork_or_upstream_rows(sqlite_db, bulk):
    user = await _user_with_every_dependent(sqlite_db, "fork-dependents")
    user_id = user.id
    assert all(await _rows_owned_by(sqlite_db, user_id)), await _rows_owned_by(sqlite_db, user_id)

    if bulk:
        await remove_users(sqlite_db, [user])
    else:
        await remove_user(sqlite_db, user)

    reused = User(username="fork-dependents-new", proxy_settings={})

    reused.id = user_id
    sqlite_db.add(reused)
    await sqlite_db.commit()
    assert reused.id == user_id
    assert await _rows_owned_by(sqlite_db, reused.id) == {
        model.__tablename__: 0 for model in (*CASCADED_MODELS, ProxySecretClaim)
    }
    claim = await sqlite_db.scalar(select(ProxySecretClaim).where(ProxySecretClaim.digest == "d" * 64))
    assert claim is not None
    assert claim.user_id is None


async def _record_usage(factory, user_id: int, rounds: int, pause: float):
    for index in range(rounds):
        async with factory() as session:
            for stmt, params in build_node_user_usage_upsert(
                "sqlite", [{"uid": user_id, "node_id": None, "created_at": USAGE_HOUR, "value": index + 1}]
            ):
                await (await session.connection()).execute(stmt, params)
            await session.commit()
        await asyncio.sleep(pause)


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign_keys", [False, True], ids=["fk-off", "fk-on"])
async def test_delete_racing_the_usage_recorder_leaves_no_rows_for_a_reused_id(tmp_path, foreign_keys):
    engine = await _engine(foreign_keys, f"sqlite+aiosqlite:///{tmp_path / 'race.db'}")
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        async with factory() as session:
            user = await _user_with_every_dependent(session, "race-victim")
            user_id = user.id

        async def delete_midway():
            await asyncio.sleep(0.02)
            async with factory() as session:
                victim = await session.get(User, user_id)
                await remove_user(session, victim)

        await asyncio.gather(_record_usage(factory, user_id, 20, 0.005), delete_midway())
        await _record_usage(factory, user_id, 3, 0)

        async with factory() as session:
            assert await session.get(User, user_id) is None
            orphans = await session.scalar(
                select(func.count()).select_from(NodeUserUsage).where(NodeUserUsage.user_id.not_in(select(User.id)))
            )
            assert orphans == 0
            reused = User(username="race-reused", proxy_settings={})
            reused.id = user_id
            session.add(reused)
            await session.commit()
            assert reused.id == user_id
            assert set((await _rows_owned_by(session, reused.id)).values()) == {0}
    finally:
        await engine.dispose()
