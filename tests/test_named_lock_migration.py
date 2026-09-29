import importlib.util
import os
from pathlib import Path

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import DateTime, String, create_engine, inspect, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

import app.db.models  # noqa: F401
from app.db.base import Base
from config import database_settings

VERSIONS = Path(__file__).resolve().parents[1] / "app" / "db" / "migrations" / "versions"
GITHUB_MODE = os.getenv("TEST_FROM", "local").lower() != "local"


def _migration_module():
    candidates = sorted(VERSIONS.glob("*_named_locks.py"))
    assert len(candidates) == 1, [path.name for path in candidates]
    spec = importlib.util.spec_from_file_location("named_locks_migration", candidates[0])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_follows_the_current_head():
    module = _migration_module()

    assert module.down_revision == "d8b2c4f60a17"


def test_the_model_declares_the_lock_table():
    table = Base.metadata.tables.get("named_locks")

    assert table is not None
    assert [column.name for column in table.primary_key.columns] == ["name"]
    assert isinstance(table.c.name.type, String)
    assert table.c.name.type.length == 64
    assert isinstance(table.c.acquired_at.type, DateTime)
    assert table.c.acquired_at.nullable is True


def test_upgrade_creates_the_seeded_table_and_downgrade_drops_it(tmp_path):
    module = _migration_module()
    engine = create_engine(f"sqlite:///{tmp_path / 'lock.db'}")
    try:
        with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
        with engine.connect() as connection:
            assert "named_locks" in inspect(connection).get_table_names()
            rows = connection.execute(text("SELECT name, acquired_at FROM named_locks")).all()
            assert [tuple(row) for row in rows] == [("proxy_secrets", None)]

        with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            module.downgrade()
        with engine.connect() as connection:
            assert "named_locks" not in inspect(connection).get_table_names()
    finally:
        engine.dispose()


def _lock_rows(connection) -> list[tuple]:
    return sorted(tuple(row) for row in connection.execute(text("SELECT name, acquired_at FROM named_locks")).all())


def _run_the_migration_in_place_of_the_current_table(connection):
    module = _migration_module()
    existed = "named_locks" in inspect(connection).get_table_names()
    held = _lock_rows(connection) if existed else []
    with Operations.context(MigrationContext.configure(connection)):
        if existed:
            module.downgrade()
        try:
            module.upgrade()
            seeded = _lock_rows(connection)
            module.downgrade()
            dropped = "named_locks" not in inspect(connection).get_table_names()
        finally:
            if existed:
                if "named_locks" not in inspect(connection).get_table_names():
                    module.upgrade()
                connection.execute(text("DELETE FROM named_locks"))
                for name, acquired_at in held:
                    connection.execute(
                        text("INSERT INTO named_locks (name, acquired_at) VALUES (:name, :acquired_at)"),
                        {"name": name, "acquired_at": acquired_at},
                    )
    return seeded, dropped


@pytest.mark.skipif(not GITHUB_MODE, reason="needs-a-server-database")
@pytest.mark.asyncio
async def test_the_migration_seeds_the_proxy_secrets_row_on_the_configured_database():
    engine = create_async_engine(database_settings.url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            before = sorted(await connection.run_sync(lambda sync: inspect(sync).get_table_names()))
            rows_before = await connection.run_sync(_lock_rows) if "named_locks" in before else []
        async with engine.begin() as connection:
            seeded, dropped = await connection.run_sync(_run_the_migration_in_place_of_the_current_table)
        async with engine.begin() as connection:
            after = sorted(await connection.run_sync(lambda sync: inspect(sync).get_table_names()))
            rows_after = await connection.run_sync(_lock_rows) if "named_locks" in after else []
    finally:
        await engine.dispose()

    assert seeded == [("proxy_secrets", None)]
    assert dropped is True
    assert after == before
    assert rows_after == rows_before
