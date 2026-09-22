import asyncio
import contextlib
import json
import logging
import os

from alembic.command import upgrade
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine, event
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from app.db import base
from config import database_settings


class TestSettings(BaseSettings):
    test_from: str = "local"
    database_url: str = "sqlite+aiosqlite:///./test.db"

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")


test_settings = TestSettings()

XRAY_JSON_TEST_FILE = "tests/api/xray_config-test.json"

TEST_FROM = test_settings.test_from
# In local mode, use in-memory SQLite by default, but allow override via DATABASE_URL env var
if TEST_FROM == "local":
    # DATABASE_URL = "sqlite+aiosqlite:///:memory:"
    DATABASE_URL = test_settings.database_url

else:
    DATABASE_URL = database_settings.url
print(f"TEST_FROM: {TEST_FROM}")
print(f"DATABASE_URL: {DATABASE_URL}")

IS_SQLITE = DATABASE_URL.startswith("sqlite")

if IS_SQLITE:
    engine = create_async_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False, "uri": True},
        poolclass=StaticPool,
        # echo=True,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _enforce_sqlite_foreign_keys(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

else:
    engine = create_async_engine(
        DATABASE_URL,
        poolclass=NullPool,  # Important for tests
        # echo=True,  # For debugging
    )
TestSession = async_sessionmaker(autocommit=False, autoflush=False, expire_on_commit=False, bind=engine)


def run_migrations_sync():
    """Run Alembic migrations synchronously."""
    try:
        # For in-memory SQLite, migrations won't work because each connection gets its own DB
        # So we skip migrations for in-memory and use create_tables instead
        if IS_SQLITE and DATABASE_URL == "sqlite+aiosqlite:///:memory:":
            print("[migrations] Skipping migrations for in-memory SQLite (will use create_tables fallback)")
            return False  # Skip migrations for in-memory SQLite

        # Use a synchronous connection for Alembic; the project env.py supports both.
        sync_db_url = DATABASE_URL
        if "sqlite+aiosqlite" in sync_db_url:
            sync_db_url = sync_db_url.replace("sqlite+aiosqlite", "sqlite")
        elif "postgresql+asyncpg" in sync_db_url:
            sync_db_url = sync_db_url.replace("postgresql+asyncpg", "postgresql")
        elif "mysql+asyncmy" in sync_db_url:
            sync_db_url = sync_db_url.replace("mysql+asyncmy", "mysql+pymysql")

        print(f"[migrations] Running migrations with database: {sync_db_url}")

        # Alembic configuration
        alembic_cfg = Config("alembic.ini")
        alembic_cfg.set_main_option("sqlalchemy.url", sync_db_url)

        # Alembic's fileConfig disables pre-existing application loggers. Keep
        # migration setup from making later log assertions depend on test order.
        logger_states = {
            logger: logger.disabled
            for logger in logging.root.manager.loggerDict.values()
            if isinstance(logger, logging.Logger)
        }
        try:
            with create_engine(sync_db_url).begin() as connection:
                alembic_cfg.attributes["connection"] = connection
                upgrade(alembic_cfg, "head")
        finally:
            for logger, disabled in logger_states.items():
                logger.disabled = disabled
        print("[migrations] Migrations completed successfully")
        return True  # Migrations ran successfully
    except Exception as e:
        print(f"[migrations] Error running migrations: {e}", file=__import__("sys").stderr)
        raise


def run_migrations():
    """Run Alembic migrations, with fallback to create_tables for in-memory SQLite."""
    migrations_ran = run_migrations_sync()
    if not migrations_ran:
        asyncio.run(create_tables())


async def create_tables():
    """Create tables using SQLAlchemy metadata (fallback method)."""
    async with engine.begin() as conn:
        await conn.run_sync(base.Base.metadata.create_all)


def _reset_local_database() -> None:
    if not IS_SQLITE or ":memory:" in DATABASE_URL:
        return

    path = DATABASE_URL.split("///", 1)[-1]
    if not path or path == ":memory:":
        return

    for leftover in (path, f"{path}-wal", f"{path}-shm"):
        with contextlib.suppress(OSError):
            os.remove(leftover)


_BOOTSTRAP_PID_ENV = "PASARGUARD_TEST_DB_BOOTSTRAP_PID"


def _bootstrap_local_database_once() -> None:
    if TEST_FROM != "local":
        return
    if os.environ.get(_BOOTSTRAP_PID_ENV) == str(os.getpid()):
        return
    os.environ[_BOOTSTRAP_PID_ENV] = str(os.getpid())
    _reset_local_database()
    run_migrations()


_bootstrap_local_database_once()


class GetTestDB:
    def __init__(self):
        self.db = TestSession()

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, exc_type, exc_value, traceback):
        if isinstance(exc_value, SQLAlchemyError):
            await self.db.rollback()  # rollback on exception

        await self.db.close()


async def get_test_db():
    async with GetTestDB() as db:
        yield db


from app import create_app

app = create_app()

app.dependency_overrides[base.get_db] = get_test_db

with open(XRAY_JSON_TEST_FILE, "w") as f:
    f.write(
        json.dumps(
            {
                "log": {"loglevel": "warning"},
                "routing": {"rules": [{"ip": ["geoip:private"], "outboundTag": "BLOCK", "type": "field"}]},
                "inbounds": [
                    {
                        "tag": "Shadowsocks TCP",
                        "listen": "0.0.0.0",
                        "port": 1080,
                        "protocol": "shadowsocks",
                        "settings": {"clients": [], "network": "tcp,udp"},
                    }
                ],
                "outbounds": [{"protocol": "freedom", "tag": "DIRECT"}, {"protocol": "blackhole", "tag": "BLOCK"}],
            },
            indent=4,
        )
    )

client = TestClient(app)
