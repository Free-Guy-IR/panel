import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.models import Admin, AdminRole, Base, Node, User
from app.jobs import cleanup_subscription_accesses, record_usages
from app.models.proxy import ProxyTable

MARIADB_DIALECT = create_async_engine("mariadb+asyncmy://user:pass@127.0.0.1/pasarguard").dialect
BUCKET = datetime(2026, 9, 23, 10, 0, tzinfo=UTC)
REAL_MARIADB_URL = os.getenv("TEST_MARIADB_URL", "")


def test_mariadb_url_resolves_to_its_own_dialect_name():
    assert MARIADB_DIALECT.name == "mariadb"


def test_the_recorder_treats_mariadb_as_the_mysql_family():
    assert record_usages.sql_family("mariadb") == "mysql"
    assert record_usages.sql_family("mysql") == "mysql"
    assert record_usages.sql_family("sqlite") == "sqlite"
    assert record_usages.sql_family("postgresql") == "postgresql"


@pytest.mark.parametrize(
    "builder, params",
    [
        (record_usages.build_node_user_usage_upsert, [{"uid": 1, "node_id": 1, "created_at": BUCKET, "value": 5}]),
        (record_usages.build_node_usage_upsert, {"node_id": 1, "created_at": BUCKET, "up": 1, "down": 2}),
    ],
)
def test_recorder_upserts_render_on_a_mariadb_url(builder, params):
    for statement, _ in builder(MARIADB_DIALECT.name, params):
        statement.compile(dialect=MARIADB_DIALECT)


class _Rows:
    def __init__(self, rows=(), rowcount=0):
        self._rows = list(rows)
        self.rowcount = rowcount

    def fetchall(self):
        return list(self._rows)

    def all(self):
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)


class _MariaDBConnection:
    def __init__(self, users: dict[int, SimpleNamespace]):
        self.users = users
        self.sent: list[tuple[str, object]] = []

    async def execute(self, statement, params=None):
        sql = str(statement.compile(dialect=MARIADB_DIALECT))
        self.sent.append((sql, params))
        if sql.lstrip().upper().startswith("SELECT") and "FOR UPDATE" in sql.upper():
            names = [column.name for column in statement.selected_columns]
            return _Rows(
                [tuple(getattr(user, name) for name in names) for user in self.users.values()],
            )
        return _Rows(rowcount=len(params) if isinstance(params, list) else 1)


class _MariaDBEngine:
    def __init__(self, connection: _MariaDBConnection):
        self.connection = connection
        self.isolation_levels: list[str] = []

    def execution_options(self, **options):
        self.isolation_levels.append(options.get("isolation_level"))
        return self

    @asynccontextmanager
    async def begin(self):
        yield self.connection


@pytest.mark.asyncio
async def test_every_billing_statement_renders_on_a_mariadb_url(monkeypatch):
    connection = _MariaDBConnection({7: SimpleNamespace(id=7, usage_epoch=0, admin_id=3)})
    fake_engine = _MariaDBEngine(connection)
    monkeypatch.setattr(record_usages, "engine", fake_engine)
    monkeypatch.setattr(record_usages, "get_dialect", AsyncMock(return_value=MARIADB_DIALECT.name))

    outcome = await record_usages.apply_fenced_user_usage(
        [{"uid": 7, "value": 1_000_000}],
        {7: record_usages.UserUsageContext(3, 0)},
        {7: 0},
        [{"uid": 7, "node_id": 1, "created_at": BUCKET, "value": 1_000_000}],
    )

    assert outcome.applied_users == 1 and outcome.applied_admins == 1
    assert fake_engine.isolation_levels == ["READ COMMITTED"]
    statements = [sql for sql, _ in connection.sent]
    assert any("ON DUPLICATE KEY UPDATE" in sql for sql in statements)
    assert not any("ON CONFLICT" in sql for sql in statements)
    billed = [params for sql, params in connection.sent if sql.startswith("UPDATE users")]
    credited = [params for sql, params in connection.sent if sql.startswith("UPDATE admins")]
    assert billed == [[{"uid": 7, "epoch": 0, "value": 1_000_000}]]
    assert credited == [[{"admin_id": 3, "value": 1_000_000}]]


class _CleanupSession:
    def __init__(self):
        self.bind = SimpleNamespace(dialect=SimpleNamespace(name=MARIADB_DIALECT.name))
        self.sent: list[str] = []
        self.committed = False

    async def execute(self, statement):
        sql = str(statement.compile(dialect=MARIADB_DIALECT))
        self.sent.append(sql)
        if "GROUP BY" in sql.upper():
            return _Rows([SimpleNamespace(user_id=5, access_kind="subscription")])
        if sql.lstrip().upper().startswith("SELECT"):
            return _Rows([SimpleNamespace(id=index) for index in range(10)])
        return _Rows(rowcount=5)

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_subscription_access_cleanup_avoids_limit_in_subquery_on_a_mariadb_url(monkeypatch):
    session = _CleanupSession()

    @asynccontextmanager
    async def fake_get_db():
        yield session

    monkeypatch.setattr(cleanup_subscription_accesses, "GetDB", fake_get_db)
    monkeypatch.setattr(cleanup_subscription_accesses, "flush_subscription_accesses", AsyncMock())
    monkeypatch.setattr(cleanup_subscription_accesses.subscription_env_settings, "access_limit", 10)

    await cleanup_subscription_accesses.cleanup_user_subscription_accesses()

    deletes = [sql for sql in session.sent if sql.lstrip().upper().startswith("DELETE")]
    assert deletes and session.committed
    assert not any("LIMIT" in sql.upper() for sql in deletes), deletes


@pytest.mark.skipif(not REAL_MARIADB_URL, reason="TEST_MARIADB_URL is not set")
@pytest.mark.asyncio
async def test_billing_survives_on_a_real_mariadb_url(monkeypatch):
    engine = create_async_engine(REAL_MARIADB_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def real_get_db():
        async with session_factory() as session:
            yield session

    monkeypatch.setattr(record_usages, "engine", engine)
    monkeypatch.setattr(record_usages, "GetDB", real_get_db)
    monkeypatch.setattr(record_usages, "_dialect_cache", [])
    try:
        async with session_factory() as db:
            role = AdminRole(name=f"role-{uuid4().hex[:6]}")
            db.add(role)
            await db.commit()
            admin = Admin(username=f"owner-{uuid4().hex[:6]}", hashed_password="x", role_id=role.id)
            node = Node(
                name="n1",
                address="127.0.0.1",
                port=62050,
                api_port=62051,
                server_ca="ca",
                api_key=str(uuid4()),
                core_config_id=None,
            )
            db.add_all([admin, node])
            await db.commit()
            user = User(username="payer", proxy_settings=ProxyTable().dict(), admin_id=admin.id)
            db.add(user)
            await db.commit()
            uid, admin_id, node_id = user.id, admin.id, node.id

        assert await record_usages.get_dialect() == "mysql"
        await record_usages.apply_fenced_user_usage(
            [{"uid": uid, "value": 1_000_000}],
            {uid: record_usages.UserUsageContext(admin_id, 0)},
            {uid: 0},
            [{"uid": uid, "node_id": node_id, "created_at": BUCKET, "value": 1_000_000}],
        )

        async with session_factory() as db:
            used = (await db.execute(select(User.used_traffic).where(User.id == uid))).scalar_one()
            credited = (await db.execute(select(Admin.used_traffic).where(Admin.id == admin_id))).scalar_one()
        assert (used, credited) == (1_000_000, 1_000_000)
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()
