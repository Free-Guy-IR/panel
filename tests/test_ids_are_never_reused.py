import logging
import sqlite3
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from alembic.command import downgrade, upgrade
from alembic.config import Config
from sqlalchemy import create_engine, select, text

from app.db import base
from app.db.crud.user import remove_user as crud_remove_user
from app.db.models import Admin, Node, User
from app.jobs import record_usages
from app.models.proxy import ProxyTable
from tests.api.helpers import unique_name
from tests.test_record_usages import DummyNode, session_factory  # noqa: F401

BEFORE_AUTOINCREMENT = "009f2a24cc7b"


def _new_user(admin_id=None) -> User:
    return User(username=unique_name("reuse_user"), admin_id=admin_id, proxy_settings=ProxyTable().dict(no_obj=True))


def _new_node() -> Node:
    return Node(
        name=unique_name("reuse_node"),
        address="10.0.0.1",
        port=1000,
        api_port=1001,
        server_ca="ca",
        api_key="key",
        core_config_id=None,
    )


async def test_the_id_of_a_deleted_newest_user_is_never_given_to_the_next_user(session_factory):  # noqa: F811
    async with session_factory() as session:
        first, newest = _new_user(), _new_user()
        session.add_all([first, newest])
        await session.commit()
        newest_id = newest.id
    async with session_factory() as session:
        await crud_remove_user(session, await session.get(User, newest_id))
    async with session_factory() as session:
        following = _new_user()
        session.add(following)
        await session.commit()
        assert following.id > newest_id, (
            f"user {newest_id} was deleted and the next user got id {following.id}; nodes key users by id, so "
            "the new user inherits the deleted user's node state"
        )


async def test_the_id_of_a_deleted_newest_node_is_never_given_to_the_next_node(session_factory):  # noqa: F811
    async with session_factory() as session:
        newest = _new_node()
        session.add(newest)
        await session.commit()
        newest_id = newest.id
    async with session_factory() as session:
        await session.delete(await session.get(Node, newest_id))
        await session.commit()
    async with session_factory() as session:
        following = _new_node()
        session.add(following)
        await session.commit()
        assert following.id > newest_id, f"node {newest_id} was deleted and the next node got id {following.id}"


async def test_usage_a_node_still_counts_for_a_deleted_user_is_never_billed_to_a_new_user(
    monkeypatch,
    session_factory,  # noqa: F811
):
    async with session_factory() as session:
        admin = Admin(username=unique_name("reuse_admin"), hashed_password="secret", role_id=3)
        session.add(admin)
        await session.flush()
        old = _new_user(admin.id)
        node = _new_node()
        session.add_all([old, node])
        await session.commit()
        old_id, node_id, admin_id = old.id, node.id, admin.id
    async with session_factory() as session:
        await crud_remove_user(session, await session.get(User, old_id))
    async with session_factory() as session:
        new = _new_user(admin_id)
        session.add(new)
        await session.commit()
        new_id = new.id

    monkeypatch.setattr(
        record_usages.node_manager, "get_healthy_nodes", AsyncMock(return_value=[(node_id, DummyNode(node_id))])
    )

    async def the_node_still_counts_the_deleted_users_traffic(node, node_id=None):
        return [{"uid": str(old_id), "value": 5_000_000}]

    monkeypatch.setattr(record_usages, "get_users_stats", the_node_still_counts_the_deleted_users_traffic)
    monkeypatch.setattr(record_usages.usage_settings, "disable_recording_node_usage", False)
    await record_usages.record_user_usages()

    async with session_factory() as session:
        billed = (await session.execute(select(User.used_traffic).where(User.id == new_id))).scalar_one()
    assert (new_id, billed) != (old_id, 5_000_000), (
        f"user {new_id} reused deleted user {old_id}'s id and was billed the node's leftover counter for it"
    )
    assert billed == 0


def _config(path) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{path}")
    return config


def _quietly(action, config: Config, revision: str) -> None:
    states = {
        logger: logger.disabled
        for logger in logging.root.manager.loggerDict.values()
        if isinstance(logger, logging.Logger)
    }
    try:
        action(config, revision)
    finally:
        for logger, disabled in states.items():
            logger.disabled = disabled


def _seed(path) -> None:
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    connection = sqlite3.connect(path)
    try:
        for user_id in (2, 5, 9):
            connection.execute(
                "INSERT INTO users (id, username, status, used_traffic, data_limit_reset_strategy, created_at, "
                "proxy_settings) VALUES (?, ?, 'active', 0, 'no_reset', ?, '{}')",
                (user_id, f"reuse-member-{user_id}", now),
            )
            connection.execute(
                "INSERT INTO user_usage_logs (user_id, used_traffic_at_reset, reset_at) VALUES (?, 5, ?)",
                (user_id, now),
            )
        connection.execute("INSERT INTO groups (id, name, is_disabled) VALUES (1, 'reuse-group', 0)")
        connection.execute("INSERT INTO users_groups_association (user_id, groups_id) VALUES (9, 1)")
        connection.execute("INSERT INTO next_plans (user_id, data_limit, add_remaining_traffic) VALUES (9, 0, 0)")
        for node_id in (3, 7):
            connection.execute(
                "INSERT INTO nodes (id, name, address, port, api_port, server_ca, api_key, status, created_at, "
                "usage_coefficient, connection_type, keep_alive, uplink, downlink) "
                "VALUES (?, ?, '10.0.0.1', 1, 2, 'ca', 'key', 'connected', ?, 1, 'grpc', 0, 0, 0)",
                (node_id, f"reuse-node-{node_id}", now),
            )
        connection.execute(
            "INSERT INTO node_user_usages (created_at, user_id, node_id, used_traffic) VALUES (?, 9, 7, 11)", (now,)
        )
        connection.commit()
    finally:
        connection.close()


def _snapshot(path) -> dict:
    connection = sqlite3.connect(path)
    try:
        return {
            "users": connection.execute("SELECT id, username FROM users ORDER BY id").fetchall(),
            "nodes": connection.execute("SELECT id, name FROM nodes ORDER BY id").fetchall(),
            "user_usage_logs": connection.execute("SELECT user_id FROM user_usage_logs ORDER BY user_id").fetchall(),
            "association": connection.execute("SELECT user_id, groups_id FROM users_groups_association").fetchall(),
            "next_plans": connection.execute("SELECT user_id FROM next_plans").fetchall(),
            "node_user_usages": connection.execute("SELECT user_id, node_id FROM node_user_usages").fetchall(),
        }
    finally:
        connection.close()


def _delete_newest_and_insert(path) -> tuple[int, int]:
    engine = create_engine(f"sqlite:///{path}")
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    with engine.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys=ON"))
        connection.execute(text("DELETE FROM users_groups_association WHERE user_id = 9"))
        connection.execute(text("DELETE FROM users WHERE id = 9"))
        connection.execute(
            text(
                "INSERT INTO users (username, status, used_traffic, data_limit_reset_strategy, created_at, "
                "proxy_settings) VALUES ('reuse-next', 'active', 0, 'no_reset', :now, '{}')"
            ),
            {"now": now},
        )
        user_id = connection.execute(text("SELECT id FROM users WHERE username = 'reuse-next'")).scalar_one()
        connection.execute(text("DELETE FROM nodes WHERE id = 7"))
        connection.execute(
            text(
                "INSERT INTO nodes (name, address, port, api_port, server_ca, api_key, status, created_at, "
                "usage_coefficient, connection_type, keep_alive, uplink, downlink) "
                "VALUES ('reuse-next', '10.0.0.1', 1, 2, 'ca', 'key', 'connected', :now, 1, 'grpc', 0, 0, 0)"
            ),
            {"now": now},
        )
        node_id = connection.execute(text("SELECT id FROM nodes WHERE name = 'reuse-next'")).scalar_one()
    engine.dispose()
    return user_id, node_id


@pytest.mark.skipif(not base.database_settings.is_sqlite, reason="id reuse is a SQLite rowid behaviour")
def test_a_migrated_sqlite_database_keeps_every_row_and_stops_reusing_ids(tmp_path):
    path = tmp_path / "reuse.db"
    config = _config(path)
    _quietly(upgrade, config, BEFORE_AUTOINCREMENT)
    _seed(path)
    before = _snapshot(path)

    _quietly(upgrade, config, "head")

    assert _snapshot(path) == before
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert dict(connection.execute("SELECT name, seq FROM sqlite_sequence WHERE name IN ('users', 'nodes')")) == {
            "users": 9,
            "nodes": 7,
        }
    finally:
        connection.close()
    assert _delete_newest_and_insert(path) == (10, 8)

    _quietly(downgrade, config, BEFORE_AUTOINCREMENT)
    _quietly(upgrade, config, "head")
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT max(id) FROM users").fetchone()[0] == 10
    finally:
        connection.close()
