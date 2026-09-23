import logging
import sqlite3
from datetime import UTC, datetime

import pytest
import sqlalchemy.ext.asyncio as sqlalchemy_asyncio
from alembic.command import upgrade
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event

from app.db import base

BEFORE_ADMIN_REBUILDS = "73c78c6a9b24"
BEFORE_LAST_ADMIN_REBUILD = "c4e81b70a935"
LAST_ADMIN_REBUILD = "b8c2d5f1a943"
USER_CHILDREN = ("next_plans", "notification_reminders", "user_usage_logs", "user_subscription_updates")
ADMIN_CASCADE_CHILDREN = ("api_keys", "admin_notification_reminders")

pytestmark = pytest.mark.skipif(
    not base.database_settings.is_sqlite, reason="the runtime engine is not SQLite, so no SQLite listener exists"
)


def _config(path) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{path}")
    return config


def _upgrade(config: Config, revision: str) -> None:
    states = {
        logger: logger.disabled
        for logger in logging.root.manager.loggerDict.values()
        if isinstance(logger, logging.Logger)
    }
    try:
        upgrade(config, revision)
    finally:
        for logger, disabled in states.items():
            logger.disabled = disabled


def _seed_user_with_children(path, *, with_group: bool) -> None:
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "INSERT INTO admins (id, username, hashed_password, created_at, is_sudo, is_disabled) "
            "VALUES (1, 'fk-owner', 'x', ?, 1, 0)",
            (now,),
        )
        connection.execute(
            "INSERT INTO users (id, username, status, used_traffic, data_limit_reset_strategy, created_at, "
            "admin_id, proxy_settings) VALUES (1, 'fk-member', 'active', 0, 'no_reset', ?, 1, '{}')",
            (now,),
        )
        connection.execute("INSERT INTO next_plans (user_id, data_limit, add_remaining_traffic) VALUES (1, 0, 0)")
        connection.execute(
            "INSERT INTO notification_reminders (user_id, type, expires_at, created_at) "
            "VALUES (1, 'expiration_date', ?, ?)",
            (now, now),
        )
        connection.execute(
            "INSERT INTO user_usage_logs (user_id, used_traffic_at_reset, reset_at) VALUES (1, 5, ?)", (now,)
        )
        connection.execute(
            "INSERT INTO user_subscription_updates (user_id, created_at, user_agent) VALUES (1, ?, 'client')",
            (now,),
        )
        if with_group:
            connection.execute("INSERT INTO groups (id, name, is_disabled) VALUES (1, 'fk-group', 0)")
            connection.execute("INSERT INTO users_groups_association (user_id, groups_id) VALUES (1, 1)")
        connection.commit()
    finally:
        connection.close()


def _seed_admin_with_cascade_children(path, *, with_user: bool) -> None:
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "INSERT INTO admin_roles (id, name, is_owner, permissions, limits, features, access, created_at, hwid) "
            "VALUES (99, 'fk-role', 1, '{}', '{}', '{}', '{}', ?, '{}')",
            (now,),
        )
        connection.execute(
            "INSERT INTO admins (id, username, hashed_password, created_at, role_id) VALUES (1, 'fk-owner', 'x', ?, 99)",
            (now,),
        )
        connection.execute(
            "INSERT INTO api_keys (admin_id, name, key_hash, api_key_trimmed, permissions, created_at) "
            "VALUES (1, 'fk-key', 'hash', 'trim', '{}', ?)",
            (now,),
        )
        connection.execute(
            "INSERT INTO admin_notification_reminders (created_at, admin_id, type) VALUES (?, 1, 'data_usage')",
            (now,),
        )
        if with_user:
            connection.execute(
                "INSERT INTO users (id, username, status, used_traffic, admin_id, proxy_settings) "
                "VALUES (1, 'fk-member', 'active', 0, 1, '{}')"
            )
            connection.execute(
                "INSERT INTO user_hwids (user_id, hwid, created_at, last_used_at) VALUES (1, 'device', ?, ?)",
                (now, now),
            )
            connection.execute("INSERT INTO next_plans (user_id, data_limit, add_remaining_traffic) VALUES (1, 0, 0)")
        connection.commit()
    finally:
        connection.close()


def _child_rows(path, tables) -> dict[str, int]:
    connection = sqlite3.connect(path)
    try:
        return {table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in tables}
    finally:
        connection.close()


@pytest.fixture
def migration_engines(monkeypatch):
    seen = []
    real = sqlalchemy_asyncio.async_engine_from_config

    def recording(*args, **kwargs):
        engine = real(*args, **kwargs)

        def read_pragma(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys")
            seen.append((engine, cursor.fetchone()[0]))
            cursor.close()

        event.listen(engine.sync_engine, "connect", read_pragma)
        return engine

    monkeypatch.setattr(sqlalchemy_asyncio, "async_engine_from_config", recording)
    return seen


def test_upgrading_through_the_admin_table_rebuilds_keeps_every_child_row(tmp_path, migration_engines):
    path = tmp_path / "migrate.db"
    config = _config(path)
    _upgrade(config, BEFORE_ADMIN_REBUILDS)
    _seed_user_with_children(path, with_group=True)
    tables = (*USER_CHILDREN, "users_groups_association", "users")
    before = _child_rows(path, tables)

    _upgrade(config, "head")

    assert before == dict.fromkeys(tables, 1)
    assert _child_rows(path, tables) == before
    assert migration_engines, "the migration did not open its own engine"
    assert {foreign_keys for _, foreign_keys in migration_engines} == {0}
    assert not any(
        event.contains(engine.sync_engine, "connect", base._set_sqlite_pragmas) for engine, _ in migration_engines
    )


def test_the_last_admin_rebuild_keeps_the_cascade_children_of_admins_and_users(tmp_path, migration_engines):
    path = tmp_path / "migrate-late.db"
    config = _config(path)
    _upgrade(config, BEFORE_LAST_ADMIN_REBUILD)
    _seed_admin_with_cascade_children(path, with_user=True)
    tables = (*ADMIN_CASCADE_CHILDREN, "users", "user_hwids", "next_plans")
    before = _child_rows(path, tables)

    _upgrade(config, "head")

    assert before == dict.fromkeys(tables, 1)
    assert _child_rows(path, tables) == before
    assert {foreign_keys for _, foreign_keys in migration_engines} == {0}


def test_the_foreign_key_pragma_is_bound_to_the_app_engine_only(tmp_path):
    assert event.contains(base.engine.sync_engine, "connect", base._set_sqlite_pragmas)
    assert not event.contains(Engine, "connect", base._set_sqlite_pragmas)

    connection = sqlite3.connect(tmp_path / "app.db")
    try:
        base._set_sqlite_pragmas(connection, None)
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        connection.close()


def test_the_same_rebuild_with_foreign_keys_on_would_lose_the_children(tmp_path):
    path = tmp_path / "control.db"
    config = _config(path)
    _upgrade(config, BEFORE_LAST_ADMIN_REBUILD)
    _seed_admin_with_cascade_children(path, with_user=False)

    enforcing = create_engine(f"sqlite:///{path}")
    event.listen(enforcing, "connect", base._set_sqlite_pragmas)
    try:
        with enforcing.begin() as connection:
            config.attributes["connection"] = connection
            _upgrade(config, LAST_ADMIN_REBUILD)
    finally:
        enforcing.dispose()

    assert _child_rows(path, ADMIN_CASCADE_CHILDREN) == dict.fromkeys(ADMIN_CASCADE_CHILDREN, 0)
