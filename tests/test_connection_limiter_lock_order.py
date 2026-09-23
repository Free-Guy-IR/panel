from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Admin, Base, User, UserStatus
from app.fork.jobs import connection_limiter as job
from app.models.proxy import ProxyTable
from app.models.settings import ConnectionLimit
from app.utils import connection_limiter as limiter


@pytest_asyncio.fixture
async def engine_and_users():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        admin = Admin(username="owner", hashed_password="x", role_id=1)
        db.add(admin)
        await db.flush()
        users = [
            User(username=f"user-{index}", proxy_settings=ProxyTable().dict(), admin_id=admin.id) for index in range(5)
        ]
        for user in users:
            user.status = UserStatus.active
        db.add_all(users)
        await db.commit()
        user_ids = [user.id for user in users]
    yield engine, factory, user_ids
    await engine.dispose()


def _observation(user_id: int):
    return SimpleNamespace(
        user_id=user_id,
        devices=1,
        address_sources=1,
        hwid_count=0,
        node_count=1,
        app_count=1,
        verdict="within_limit",
        limit_applied=2,
        node_streak=0,
        at_once_streak=0,
        streak=0,
        reasons=[],
        details={},
    )


@pytest.mark.asyncio
async def test_connection_states_are_inserted_in_user_id_order_whatever_the_observation_order(engine_and_users):
    engine, factory, user_ids = engine_and_users
    scrambled = [user_ids[3], user_ids[0], user_ids[4], user_ids[1], user_ids[2]]
    inserted: list[int] = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if " ".join(statement.upper().split()).startswith("INSERT INTO USER_CONNECTION_STATES"):
            columns = [name.strip() for name in statement.split("(", 1)[1].split(")", 1)[0].split(",")]
            rows = parameters if parameters and isinstance(parameters[0], (tuple, list)) else [parameters]
            for row in rows:
                values = list(row)
                inserted.extend(
                    values[offset + columns.index("user_id")] for offset in range(0, len(values), len(columns))
                )

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        async with factory() as db:
            await job._write_states(db, [_observation(user_id) for user_id in scrambled])
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)

    assert inserted == sorted(user_ids)


@pytest.mark.asyncio
async def test_the_prune_transaction_is_committed_even_when_it_deleted_nothing(engine_and_users, monkeypatch):
    _, factory, _ = engine_and_users
    settings = ConnectionLimit(enabled=True, enforcement_enabled=False, check_interval_seconds=30)
    seen: dict[str, bool] = {}

    class _GetDB:
        async def __aenter__(self):
            self.db = factory()
            return self.db

        async def __aexit__(self, *exc):
            await self.db.close()

    async def assessment(db, _settings):
        seen["in_transaction"] = db.in_transaction()
        return []

    monkeypatch.setattr(job, "GetDB", _GetDB)
    monkeypatch.setattr(job, "_current_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(job, "_release_due", AsyncMock())
    monkeypatch.setattr(job, "_last_assessment", 0.0)
    monkeypatch.setattr(job, "_seconds_since_last_check", AsyncMock(return_value=None))
    monkeypatch.setattr(job, "run_assessment", assessment)

    await job.record_connection_states()

    assert seen == {"in_transaction": False}


@pytest.mark.asyncio
async def test_no_transaction_is_open_while_the_nodes_are_asked_for_addresses(engine_and_users, monkeypatch):
    _, factory, user_ids = engine_and_users
    settings = ConnectionLimit(enabled=True, online_window_seconds=600)
    seen: dict[str, bool] = {}

    async def online_users(db, _settings):
        return list((await db.execute(select(User).where(User.id.in_(user_ids)))).scalars().all())

    async def live_ips(users, by_node, *args, **kwargs):
        seen["in_transaction"] = db.in_transaction()
        return {}, {}

    monkeypatch.setattr(limiter, "candidate_users", online_users)
    monkeypatch.setattr(limiter, "collect_live_ips", live_ips)

    async with factory() as db:
        observations = await limiter.run_assessment(db, settings)

    assert seen == {"in_transaction": False}
    assert sorted(o.user_id for o in observations) == sorted(user_ids)
