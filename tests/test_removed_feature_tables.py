import shutil
import subprocess
from pathlib import Path

from alembic.command import check, upgrade
from sqlalchemy import create_engine, inspect

from app.fork.db_rollback import default_config
from app.fork.migration_guards import MIGRATION_OWNED_TABLES

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = Path("app") / "db" / "migrations"
PRE_REMOVAL_COMMIT = "eab8d58f5c6a377b82ef58e483f23dc0f9c76ec0"
PRE_REMOVAL_REVISION = "c7f1a9d3e604"
PRE_REMOVAL_FILES = (
    "a7d5e1f3b294_traffic_log_tables.py",
    "b8e6f2a4c517_traffic_log_retention.py",
    "c3f81ab7d420_add_content_filter_tables.py",
    "c7f1a9d3e604_content_filter_unique_fleet_wide_scope.py",
    "d4a92bc8e531_content_filter_fleet_wide_inbound_scope.py",
    "e5b3c9d1f072_content_filter_sniffing_overrides.py",
    "f6c4d0e2a183_content_filter_backfill_sniffing_owners.py",
)
GUTTED_BODY = "def upgrade() -> None:\n    pass\n"
LIVE_PARENTS = {"nodes", "core_configs"}
DEAD_FOREIGN_KEYS = {
    "fk_content_filter_assignments_node_id_nodes",
    "fk_content_filter_sniffing_overrides_core_id_core_configs",
}
SEEDS = (
    "INSERT INTO core_configs (id, created_at, name, config) VALUES (500, '2026-09-20 00:00:00', 'kept', '{}')",
    (
        "INSERT INTO content_filter_profiles (id, name, created_at, updated_at, categories, allow_list, block_list, "
        "strict_mode, note) VALUES (7, 'kids', '2026-09-20 00:00:00', '2026-09-20 00:00:00', '[\"adult\"]', '[]', "
        "'[\"example.com\"]', 1, 'kept across the removal')"
    ),
    (
        "INSERT INTO content_filter_assignments (id, profile_id, node_id, inbound_tag, is_enabled, enforced, "
        "applied_digest) VALUES (3, 7, NULL, 'fleet', 1, 0, 'digest')"
    ),
    (
        "INSERT INTO content_filter_sniffing_overrides (id, core_id, inbound_tag, original, installed_at) "
        "VALUES (4, 500, 'fleet', '{\"enabled\": true}', '2026-09-20 00:00:00')"
    ),
    (
        "INSERT INTO content_filter_sniffing_repairs (id, core_id, inbound_tag, action, previous_original, applied_at) "
        "VALUES (5, 500, 'fleet', 'rewritten', '{\"enabled\": false}', '2026-09-20 00:00:00')"
    ),
    (
        "INSERT INTO traffic_log_identities (user_id, username, admin_id, deleted, updated_at) "
        "VALUES (11, 'u11', NULL, 0, '2026-09-20 00:00:00')"
    ),
    (
        "INSERT INTO traffic_log_records (id, bucket_start, user_id, user_label, node_id, inbound_tag, host, port, "
        "protocol, refused, route, first_seen, last_seen, hits) VALUES (12, '2026-09-20 00:00:00', 11, 'u11', 1, "
        "'fleet', 'example.com', 443, 'tcp', 0, 'direct', '2026-09-20 00:00:00', '2026-09-20 00:00:00', 9)"
    ),
    "INSERT INTO traffic_log_state (id, enabled, updated_at, retention_hours) VALUES (9, 1, '2026-09-20 00:00:00', 72)",
)


def _database(tmp_path, name):
    path = tmp_path / name
    return default_config(f"sqlite+aiosqlite:///{path}"), f"sqlite:///{path}"


def _pre_removal_migrations(tmp_path):
    assert not subprocess.run(
        ["git", "cat-file", "-e", f"{PRE_REMOVAL_COMMIT}^{{commit}}"], cwd=ROOT, capture_output=True, check=False
    ).returncode, f"the pre-removal commit {PRE_REMOVAL_COMMIT} is not in this checkout; fetch the full history"
    tree = tmp_path / "pre-removal-migrations"
    shutil.copytree(ROOT / MIGRATIONS, tree, ignore=shutil.ignore_patterns("__pycache__"))
    for name in PRE_REMOVAL_FILES:
        path = (MIGRATIONS / "versions" / name).as_posix()
        body = subprocess.run(
            ["git", "show", f"{PRE_REMOVAL_COMMIT}:{path}"], cwd=ROOT, capture_output=True, check=True, text=True
        ).stdout
        assert GUTTED_BODY not in body, f"{name} is already gutted at {PRE_REMOVAL_COMMIT}"
        (tree / "versions" / name).write_text(body)
    return tree


def _snapshot(url):
    shape = {}
    with create_engine(url).connect() as connection:
        for table in MIGRATION_OWNED_TABLES:
            definition = connection.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
            ).scalar()
            if definition is None:
                continue
            rows = sorted(map(repr, connection.exec_driver_sql(f"SELECT * FROM {table}").all()))
            shape[table] = (definition, rows)
    return shape


def test_a_fresh_install_creates_no_table_of_a_removed_feature(tmp_path):
    config, url = _database(tmp_path, "fresh.sqlite3")
    upgrade(config, "heads")

    inspector = inspect(create_engine(url))
    tables = set(inspector.get_table_names())
    assert sorted(set(MIGRATION_OWNED_TABLES) & tables) == []

    reaching = sorted(
        (table, key["name"], key["referred_table"])
        for table in tables
        for key in inspector.get_foreign_keys(table)
        if key["referred_table"] in LIVE_PARENTS
        and (table in MIGRATION_OWNED_TABLES or key["name"] in DEAD_FOREIGN_KEYS)
    )
    assert reaching == []

    check(config)


def test_an_upgraded_database_keeps_every_removed_feature_table_with_its_rows(tmp_path):
    config, url = _database(tmp_path, "upgraded.sqlite3")
    pre_removal = default_config(config.get_main_option("sqlalchemy.url"))
    pre_removal.set_main_option("script_location", str(_pre_removal_migrations(tmp_path)))
    upgrade(pre_removal, PRE_REMOVAL_REVISION)
    with create_engine(url).begin() as connection:
        for statement in SEEDS:
            connection.exec_driver_sql(statement)

    before = _snapshot(url)
    assert sorted(before) == sorted(MIGRATION_OWNED_TABLES)
    assert all(rows for _definition, rows in before.values())
    assert DEAD_FOREIGN_KEYS <= {
        name for definition, _rows in before.values() for name in DEAD_FOREIGN_KEYS if name in definition
    }

    upgrade(config, "heads")

    assert _snapshot(url) == before
    check(config)
