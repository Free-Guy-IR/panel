import os

import pytest
from alembic.command import upgrade
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from app.fork.db_rollback import rollback
from tests.test_upstream_v551_migration_splice import (
    FORK_HEAD,
    HEAD,
    _config,
    _quiet,
    _scratch_database,
    _sync,
    _versions,
)

SORT_ORDER_PARENT = "86f7c2a14d3e"
SORT_ORDER_REVISION = "d73f8a2c4e91"
SORT_ORDER_TABLES = ("nodes", "core_configs")
ASSOCIATION_TABLES = ("inbounds_groups_association", "users_groups_association")

pytestmark = pytest.mark.skipif(
    make_url(os.environ.get("SPLICE_SCRATCH_URL", "sqlite://")).get_backend_name() not in ("mysql", "mariadb"),
    reason="only MySQL and MariaDB commit DDL outside the migration transaction",
)


def _execute(url: str, *statements: str) -> None:
    engine = create_engine(_sync(url))
    try:
        with engine.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))
    finally:
        engine.dispose()


def _sort_order_columns(url: str) -> dict[str, bool]:
    engine = create_engine(_sync(url))
    try:
        inspector = inspect(engine)
        return {
            table: "sort_order" in {column["name"] for column in inspector.get_columns(table)}
            for table in SORT_ORDER_TABLES
        }
    finally:
        engine.dispose()


def _association_indexes(url: str) -> dict[str, list]:
    engine = create_engine(_sync(url))
    try:
        inspector = inspect(engine)
        return {
            table: sorted((index["name"], tuple(index["column_names"])) for index in inspector.get_indexes(table))
            for table in ASSOCIATION_TABLES
        }
    finally:
        engine.dispose()


def _rollback(config, target: str) -> None:
    _quiet(rollback, config, target, echo=lambda _line: None)


def test_an_upgrade_interrupted_inside_the_sort_order_revision_completes_on_the_next_run(tmp_path):
    with _scratch_database(tmp_path, "sort_order_up") as url:
        config = _config(url)
        _quiet(upgrade, config, FORK_HEAD)
        _quiet(upgrade, config, SORT_ORDER_PARENT)
        _execute(url, "ALTER TABLE nodes ADD COLUMN sort_order INTEGER NULL", "UPDATE nodes SET sort_order = id")

        _quiet(upgrade, config, "head")

        assert _versions(url) == [HEAD]
        assert _sort_order_columns(url) == {"nodes": True, "core_configs": True}


def test_a_rollback_interrupted_inside_the_sort_order_revision_completes_on_the_next_run(tmp_path):
    with _scratch_database(tmp_path, "sort_order_down") as url:
        config = _config(url)
        _quiet(upgrade, config, FORK_HEAD)
        _quiet(upgrade, config, SORT_ORDER_REVISION)
        _execute(url, "ALTER TABLE core_configs DROP COLUMN sort_order")

        _rollback(config, FORK_HEAD)

        assert _versions(url) == [FORK_HEAD]
        assert _sort_order_columns(url) == {"nodes": False, "core_configs": False}


def test_a_rollback_of_a_migration_built_database_restores_the_foreign_key_indexes(tmp_path):
    with _scratch_database(tmp_path, "group_fk_index") as url:
        config = _config(url)
        _quiet(upgrade, config, FORK_HEAD)
        before = _association_indexes(url)
        _quiet(upgrade, config, "head")

        _rollback(config, FORK_HEAD)

        assert _versions(url) == [FORK_HEAD]
        assert _association_indexes(url) == before
        _quiet(upgrade, config, "head")
        assert _versions(url) == [HEAD]
