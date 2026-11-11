from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.models import NodeUserUsage
from app.jobs import record_usages
from tests.test_recorder_harness import _database_url, recorder_db_fixture, seed_users_and_nodes  # noqa: F401

BUCKET = datetime(2026, 10, 26, 10, 0, tzinfo=UTC)
VALUES_FUNCTION = re.compile(r"VALUES\s*\(\s*`?\w+`?\s*\)", re.IGNORECASE)


def _mysql_dialect(version: tuple[int, ...]):
    dialect = mysql.dialect()
    dialect.server_version_info = version
    dialect._requires_alias_for_on_duplicate_key = version >= (8, 0, 20)
    return dialect


def _mariadb_dialect():
    return create_async_engine("mariadb+asyncmy://user:pass@127.0.0.1/pasarguard").dialect


def _rows(*pairs):
    return [{"uid": uid, "node_id": node_id, "created_at": BUCKET, "value": value} for uid, node_id, value in pairs]


@pytest.mark.parametrize("dialect", [_mysql_dialect((8, 0, 20)), _mysql_dialect((9, 7, 2)), _mariadb_dialect()])
def test_the_node_user_usage_upsert_does_not_use_the_deprecated_values_function(dialect):
    statements = record_usages.build_node_user_usage_upsert(dialect.name, _rows((1, 2, 5), (3, None, 7)))
    for statement, _ in statements:
        sql = str(statement.compile(dialect=dialect))
        update_clause = sql.split("ON DUPLICATE KEY UPDATE", 1)[1]
        assert not VALUES_FUNCTION.search(update_clause), sql
        assert "insert_source" in update_clause, sql


def test_the_node_usage_upsert_uses_the_row_alias_on_mysql_8_0_20_and_later():
    dialect = _mysql_dialect((9, 7, 2))
    for statement, _ in record_usages.build_node_usage_upsert(
        "mysql", {"node_id": 1, "created_at": BUCKET, "up": 1, "down": 2}
    ):
        sql = str(statement.compile(dialect=dialect))
        assert not VALUES_FUNCTION.search(sql.split("ON DUPLICATE KEY UPDATE", 1)[1]), sql


@pytest.mark.skipif(not _database_url().startswith("mysql"), reason="needs a MySQL or MariaDB server")
@pytest.mark.asyncio
async def test_repeated_node_user_usage_upserts_add_up_without_server_warnings(recorder_db):
    _, user_ids, node_ids = await seed_users_and_nodes(recorder_db, user_count=2, node_count=2)
    engine = record_usages.engine
    dialect = engine.dialect.name
    warnings = []
    for values in ((100, 40), (50, 2)):
        rows = _rows((user_ids[0], node_ids[0], values[0]), (user_ids[1], node_ids[1], values[1]))
        async with engine.begin() as conn:
            for statement, params in record_usages.build_node_user_usage_upsert(dialect, rows):
                await conn.execute(statement, params)
                warnings.extend((await conn.execute(text("SHOW WARNINGS"))).all())

    async with recorder_db() as session:
        written = (
            await session.execute(select(NodeUserUsage.user_id, NodeUserUsage.node_id, NodeUserUsage.used_traffic))
        ).all()
    assert [tuple(warning) for warning in warnings] == []
    assert sorted(written, key=lambda row: row[0]) == [(user_ids[0], node_ids[0], 150), (user_ids[1], node_ids[1], 42)]
