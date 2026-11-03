import json
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic.command import check, upgrade
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from app.db.models import Base
from app.fork.db_rollback import main
from app.fork.migration_guards import (
    REVERSE_GROUP_FOREIGN_KEYS,
    REVERSE_GROUP_INDEX_REVISION,
    REVISION_COLUMNS,
    REVISION_OBJECTS,
    SORT_ORDER_REVISION,
    SORT_ORDER_TABLES,
    StrayMigrationObjectError,
    _restore_applied_objects,
)
from tests.test_mysql_interrupted_migrations import _execute, _rollback
from tests.test_upstream_v551_migration_splice import (
    HEAD,
    PROD_REVISION,
    _config,
    _quiet,
    _scratch_database,
    _sync,
    _versions,
)

OBJECTS = {revision: (tables, indexes) for revision, tables, indexes in REVISION_OBJECTS}
COLUMNS = dict(REVISION_COLUMNS)
NODE_STATS_FOREIGN_KEY_INDEX = "CREATE INDEX fk_node_stats_node_id_nodes ON node_stats (node_id)"
COLUMN_UPGRADE_CUTS = (
    ("f0420da90ff1", ("ALTER TABLE settings ADD COLUMN cleanup JSON NULL",)),
    ("1a2b3c4d5e6f", ("ALTER TABLE api_keys ADD COLUMN max_requests INTEGER NULL",)),
    (
        "1a2b3c4d5e6f",
        (
            "ALTER TABLE api_keys ADD COLUMN max_requests INTEGER NULL",
            "ALTER TABLE api_keys ADD COLUMN request_count INTEGER NOT NULL DEFAULT '0'",
        ),
    ),
)
COLUMN_DOWNGRADE_CUTS = (
    ("f0420da90ff1", (NODE_STATS_FOREIGN_KEY_INDEX, "DROP INDEX ix_node_stats_node_id_created_at ON node_stats")),
    (
        "f0420da90ff1",
        (
            NODE_STATS_FOREIGN_KEY_INDEX,
            "DROP INDEX ix_node_stats_node_id_created_at ON node_stats",
            "DROP INDEX ix_node_stats_created_at ON node_stats",
            "ALTER TABLE settings DROP COLUMN cleanup",
        ),
    ),
    ("1a2b3c4d5e6f", ("ALTER TABLE api_keys DROP COLUMN delete_on_limit",)),
    (
        "1a2b3c4d5e6f",
        ("ALTER TABLE api_keys DROP COLUMN delete_on_limit", "ALTER TABLE api_keys DROP COLUMN request_count"),
    ),
)
DOWNGRADE_CUTS = (
    ("9a5c2e7f1b4d", "DROP TABLE passkey_challenges"),
    ("e5a9c3f1b7d2", "DROP TABLE named_locks"),
    ("b7e2c4d91f60", "DROP INDEX idx_users_created_at_id ON users"),
    ("86f7c2a14d3e", "DROP INDEX idx_user_subscription_updates_created_at ON user_subscription_updates"),
    ("b176d23e434c", "DROP TABLE pending_user_resyncs"),
    ("6d8eb8fba21b", "DROP INDEX ix_node_stat_acks_node_id_stat_type ON node_stat_acks"),
    ("009f2a24cc7b", "DROP INDEX ix_pending_node_removals_requested_at ON pending_node_removals"),
    ("e79833e2a0b2", "DROP TABLE proxy_secret_claims"),
    ("e5a7c3f9b214", "DROP TABLE node_outbound_usages"),
)

mysql_only = pytest.mark.skipif(
    make_url(os.environ.get("SPLICE_SCRATCH_URL", "sqlite://")).get_backend_name() not in ("mysql", "mariadb"),
    reason="only MySQL and MariaDB commit DDL outside the migration transaction",
)


def _script() -> ScriptDirectory:
    return ScriptDirectory.from_config(_config("sqlite://"))


def _parent(revision: str) -> str:
    return _script().get_revision(revision).down_revision


def _upgrade_order() -> list[str]:
    script = _script()
    production = {revision.revision for revision in script.iterate_revisions(PROD_REVISION, "base")}
    walked = reversed(list(script.walk_revisions("base", "heads")))
    return [revision.revision for revision in walked if revision.revision not in production]


def _objects(url: str) -> dict[str, set[str]]:
    engine = create_engine(_sync(url))
    try:
        inspector = inspect(engine)
        return {
            table: {index["name"] for index in inspector.get_indexes(table)} for table in inspector.get_table_names()
        }
    finally:
        engine.dispose()


def _guarded_objects() -> dict[str, tuple[set[str], set[tuple[str, str]]]]:
    guarded = {
        revision: (set(tables), {(table, name) for table, name, _columns in indexes})
        for revision, tables, indexes in REVISION_OBJECTS
    }
    guarded[REVERSE_GROUP_INDEX_REVISION] = (
        set(),
        {(table, index) for table, _column, index in REVERSE_GROUP_FOREIGN_KEYS},
    )
    return guarded


def _build(url: str, revision: str) -> None:
    tables, indexes = OBJECTS[revision]
    engine = create_engine(_sync(url))
    try:
        with engine.begin() as connection:
            for table in tables:
                Base.metadata.tables[table].create(connection)
        inspector = inspect(engine)
        missing = [
            f"CREATE INDEX {name} ON {table} ({', '.join(columns)})"
            for table, name, columns in indexes
            if name not in {index["name"] for index in inspector.get_indexes(table)}
        ]
    finally:
        engine.dispose()
    _execute(url, *missing)


def _columns(url: str) -> dict[str, set[str]]:
    engine = create_engine(_sync(url))
    try:
        inspector = inspect(engine)
        return {
            table: {column["name"] for column in inspector.get_columns(table)} for table in inspector.get_table_names()
        }
    finally:
        engine.dispose()


def _absent(url: str, revision: str) -> bool:
    tables, indexes = OBJECTS.get(revision, ((), ()))
    objects = _objects(url)
    columns = _columns(url)
    return (
        not any(table in objects for table in tables)
        and not any(name in objects.get(table, set()) for table, name, _columns in indexes)
        and not any(column in columns.get(table, set()) for table, column in COLUMNS.get(revision, ()))
    )


def _present(url: str, revision: str) -> bool:
    tables, indexes = OBJECTS.get(revision, ((), ()))
    objects = _objects(url)
    columns = _columns(url)
    return (
        all(table in objects for table in tables)
        and all(name in objects.get(table, set()) for table, name, _columns in indexes)
        and all(column in columns.get(table, set()) for table, column in COLUMNS.get(revision, ()))
    )


def _node_stats_indexes(url: str) -> set[str]:
    return _objects(url)["node_stats"]


def _cleanup_values(url: str) -> list:
    engine = create_engine(_sync(url))
    try:
        with engine.connect() as connection:
            return connection.execute(text("SELECT cleanup FROM settings")).scalars().all()
    finally:
        engine.dispose()


def _lock_names(url: str) -> list[str]:
    engine = create_engine(_sync(url))
    try:
        with engine.connect() as connection:
            return connection.execute(text("SELECT name FROM named_locks ORDER BY name")).scalars().all()
    finally:
        engine.dispose()


def _assert_clean_head(url: str) -> None:
    assert _versions(url) == [HEAD]
    _quiet(check, _config(url))


def test_every_table_and_index_created_above_production_is_owned_by_a_migration_guard(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'objects.sqlite3'}"
    config = _config(url)
    _quiet(upgrade, config, PROD_REVISION)
    observed = {}
    before = _objects(url)
    for revision in _upgrade_order():
        _quiet(upgrade, config, revision)
        after = _objects(url)
        tables = set(after) - set(before)
        indexes = {(table, name) for table, names in after.items() for name in names - before.get(table, set())}
        if tables or indexes:
            observed[revision] = (tables, indexes)
        before = after
    assert observed == _guarded_objects()


def test_every_column_added_above_production_is_owned_by_a_migration_guard(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'columns.sqlite3'}"
    config = _config(url)
    _quiet(upgrade, config, PROD_REVISION)
    observed = {}
    before = _columns(url)
    for revision in _upgrade_order():
        _quiet(upgrade, config, revision)
        after = _columns(url)
        added = {
            (table, column) for table, names in after.items() if table in before for column in names - before[table]
        }
        if added:
            observed[revision] = added
        before = after
    guarded = {revision: set(columns) for revision, columns in REVISION_COLUMNS}
    guarded[SORT_ORDER_REVISION] = {(table, "sort_order") for table in SORT_ORDER_TABLES}
    assert observed == guarded


@mysql_only
def test_a_rollback_to_production_removes_a_stray_named_locks_table_and_the_next_upgrade_reaches_head(tmp_path):
    with _scratch_database(tmp_path, "stray_locks") as url:
        _quiet(upgrade, _config(url), PROD_REVISION)
        _build(url, "e5a9c3f1b7d2")
        assert _quiet(main, ["--to", PROD_REVISION, "--url", url]) == 0
        assert _versions(url) == [PROD_REVISION]
        assert _absent(url, "e5a9c3f1b7d2")
        _quiet(upgrade, _config(url), "head")
        _assert_clean_head(url)
        assert _lock_names(url) == ["proxy_secrets"]


@mysql_only
@pytest.mark.parametrize("revision", list(OBJECTS))
def test_an_upgrade_cut_after_the_revision_ddl_is_finished_by_the_next_upgrade(tmp_path, revision):
    with _scratch_database(tmp_path, f"upgrade_cut_{revision}") as url:
        parent = _parent(revision)
        _quiet(upgrade, _config(url), parent)
        _build(url, revision)
        assert _versions(url) == [parent]
        _quiet(upgrade, _config(url), "head")
        _assert_clean_head(url)
        assert _present(url, revision)
        assert _lock_names(url) == ["proxy_secrets"]


@mysql_only
def test_a_rollback_removes_a_stray_proxy_secret_claims_table_left_by_a_cut_upgrade(tmp_path):
    with _scratch_database(tmp_path, "stray_claims") as url:
        _quiet(upgrade, _config(url), "fd192e9628a5")
        _build(url, "e79833e2a0b2")
        _rollback(_config(url), "e5a9c3f1b7d2")
        assert _versions(url) == ["e5a9c3f1b7d2"]
        assert _absent(url, "e79833e2a0b2")


@mysql_only
def test_a_stray_table_holding_rows_is_refused_and_nothing_is_changed(tmp_path):
    with _scratch_database(tmp_path, "stray_rows") as url:
        _quiet(upgrade, _config(url), PROD_REVISION)
        _build(url, "e5a9c3f1b7d2")
        _build(url, "009f2a24cc7b")
        _execute(url, "INSERT INTO named_locks (name) VALUES ('kept')")
        with pytest.raises(
            StrayMigrationObjectError,
            match=r"table named_locks holds 1 rows although revision e5a9c3f1b7d2 is not applied; nothing was changed",
        ):
            _quiet(upgrade, _config(url), "head")
        assert _versions(url) == [PROD_REVISION]
        assert _present(url, "e5a9c3f1b7d2")
        assert _present(url, "009f2a24cc7b")
        engine = create_engine(_sync(url))
        try:
            with engine.connect() as connection:
                assert connection.execute(text("SELECT name FROM named_locks")).scalars().all() == ["kept"]
        finally:
            engine.dispose()


@mysql_only
@pytest.mark.parametrize(("revision", "statement"), DOWNGRADE_CUTS)
def test_a_rollback_cut_after_the_revision_dropped_its_first_object_completes(tmp_path, revision, statement):
    with _scratch_database(tmp_path, f"downgrade_cut_{revision}") as url:
        parent = _parent(revision)
        _quiet(upgrade, _config(url), revision)
        _execute(url, statement)
        _rollback(_config(url), parent)
        assert _versions(url) == [parent]
        assert _absent(url, revision)


@mysql_only
@pytest.mark.parametrize(("revision", "statement"), DOWNGRADE_CUTS)
def test_an_upgrade_after_a_rollback_cut_rebuilds_the_dropped_object(tmp_path, revision, statement):
    with _scratch_database(tmp_path, f"rebuild_{revision}") as url:
        _quiet(upgrade, _config(url), revision)
        _execute(url, statement)
        _quiet(upgrade, _config(url), "head")
        _assert_clean_head(url)
        assert _present(url, revision)
        assert _lock_names(url) == ["proxy_secrets"]


@mysql_only
@pytest.mark.parametrize(("revision", "statements"), COLUMN_UPGRADE_CUTS)
def test_an_upgrade_cut_after_some_added_columns_is_finished_by_the_next_upgrade(tmp_path, revision, statements):
    with _scratch_database(tmp_path, f"column_upgrade_cut_{revision}_{len(statements)}") as url:
        parent = _parent(revision)
        _quiet(upgrade, _config(url), parent)
        _execute(url, *statements)
        assert _versions(url) == [parent]
        _quiet(upgrade, _config(url), "head")
        _assert_clean_head(url)
        assert _present(url, revision)
        assert _cleanup_values(url) == [{}]


@mysql_only
@pytest.mark.parametrize(("revision", "statements"), COLUMN_DOWNGRADE_CUTS)
def test_a_rollback_cut_after_some_dropped_columns_completes(tmp_path, revision, statements):
    with _scratch_database(tmp_path, f"column_downgrade_cut_{revision}_{len(statements)}") as url:
        parent = _parent(revision)
        _quiet(upgrade, _config(url), revision)
        _execute(url, *statements)
        _rollback(_config(url), parent)
        assert _versions(url) == [parent]
        assert _absent(url, revision)


@mysql_only
@pytest.mark.parametrize(("revision", "statements"), COLUMN_DOWNGRADE_CUTS)
def test_an_upgrade_after_a_rollback_cut_restores_the_dropped_columns(tmp_path, revision, statements):
    with _scratch_database(tmp_path, f"column_rebuild_{revision}_{len(statements)}") as url:
        _quiet(upgrade, _config(url), revision)
        _execute(url, *statements)
        _quiet(upgrade, _config(url), "head")
        _assert_clean_head(url)
        assert _present(url, revision)
        assert _cleanup_values(url) == [{}]
        assert "fk_node_stats_node_id_nodes" not in _node_stats_indexes(url)


@mysql_only
def test_a_rollback_removes_a_stray_node_stats_index_pair_without_losing_the_foreign_key_index(tmp_path):
    with _scratch_database(tmp_path, "node_stats_pair") as url:
        parent = _parent("f0420da90ff1")
        _quiet(upgrade, _config(url), parent)
        _build(url, "f0420da90ff1")
        assert "fk_node_stats_node_id_nodes" not in _node_stats_indexes(url)
        _rollback(_config(url), _parent(parent))
        assert _absent(url, "f0420da90ff1")
        assert "fk_node_stats_node_id_nodes" in _node_stats_indexes(url)
        _quiet(upgrade, _config(url), "head")
        _assert_clean_head(url)
        assert "fk_node_stats_node_id_nodes" not in _node_stats_indexes(url)


def _claim_count(url: str) -> int:
    engine = create_engine(_sync(url))
    try:
        with engine.connect() as connection:
            return connection.execute(text("SELECT COUNT(*) FROM proxy_secret_claims")).scalar()
    finally:
        engine.dispose()


def _restore(url: str, removed: set[str]) -> None:
    script = _script()
    applied = {revision.revision for revision in script.walk_revisions("base", "heads")}
    engine = create_engine(_sync(url))
    try:
        with engine.begin() as connection:
            _restore_applied_objects(connection, script, applied, removed)
    finally:
        engine.dispose()


def test_a_claims_table_recreated_by_an_abandoned_rollback_is_backfilled_by_the_next_upgrade(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'claims.sqlite3'}"
    _quiet(upgrade, _config(url), "head")
    settings = {"vless": {"id": str(uuid4())}, "trojan": {"password": "claims-trojan-secret"}}
    _execute(
        url,
        "INSERT INTO users (username, status, used_traffic, data_limit_reset_strategy, created_at, proxy_settings) "
        f"VALUES ('claims', 'active', 0, 'no_reset', '{datetime.now(UTC):%Y-%m-%d %H:%M:%S}', '{json.dumps(settings)}')",
        "DROP TABLE proxy_secret_claims",
    )
    _restore(url, {"e79833e2a0b2"})
    assert _claim_count(url) == 0
    _restore(url, set())
    assert _claim_count(url) == 2
