import os

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
    REVISION_OBJECTS,
    StrayMigrationObjectError,
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
DOWNGRADE_CUTS = (
    ("9a5c2e7f1b4d", "DROP TABLE passkey_challenges"),
    ("e5a9c3f1b7d2", "DROP TABLE named_locks"),
    ("b7e2c4d91f60", "DROP INDEX idx_users_created_at_id ON users"),
    ("86f7c2a14d3e", "DROP INDEX idx_user_subscription_updates_created_at ON user_subscription_updates"),
    ("b176d23e434c", "DROP TABLE pending_user_resyncs"),
    ("6d8eb8fba21b", "DROP INDEX ix_node_stat_acks_node_id_stat_type ON node_stat_acks"),
    ("009f2a24cc7b", "DROP INDEX ix_pending_node_removals_requested_at ON pending_node_removals"),
    ("e79833e2a0b2", "DROP TABLE proxy_secret_claims"),
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


def _absent(url: str, revision: str) -> bool:
    tables, indexes = OBJECTS[revision]
    objects = _objects(url)
    return not any(table in objects for table in tables) and not any(
        name in objects.get(table, set()) for table, name, _columns in indexes
    )


def _present(url: str, revision: str) -> bool:
    tables, indexes = OBJECTS[revision]
    objects = _objects(url)
    return all(table in objects for table in tables) and all(
        name in objects.get(table, set()) for table, name, _columns in indexes
    )


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
