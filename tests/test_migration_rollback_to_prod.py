import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic.command import downgrade, upgrade
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from app.fork.db_rollback import RollbackError, current_heads, default_config, main, rollback

ROOT = Path(__file__).resolve().parents[1]
PROD_REVISION = "e5a9c3f1b7d2"
PROD_COMMIT = "d46a03b65e2afc126aa18ed11638ec143585f725"
PROD_GRAPH_SIZE = 181
PROD_GRAPH_FINGERPRINT = "d79312d7f29e86ea99c3c6f540ad366673bbe6c109d4c576c2e3e7551b63e040"
PASSKEY_TABLES = {"admin_passkeys", "passkey_challenges"}
RELEASE_ONLY_TABLES = {"pending_user_resyncs", "node_stat_acks"}


def _database(tmp_path, name="rollback.sqlite3"):
    path = tmp_path / name
    return default_config(f"sqlite+aiosqlite:///{path}"), f"sqlite:///{path}"


def _versions(sync_url):
    with create_engine(sync_url).connect() as connection:
        return sorted(row[0] for row in connection.execute(text("SELECT version_num FROM alembic_version")))


def _schema(sync_url):
    inspector = inspect(create_engine(sync_url))
    shape = {}
    for table in inspector.get_table_names():
        if table == "alembic_version":
            continue
        shape[table] = (
            sorted(
                (column["name"], str(column["type"]), column["nullable"]) for column in inspector.get_columns(table)
            ),
            sorted(
                (index["name"], tuple(index["column_names"]), bool(index["unique"]))
                for index in inspector.get_indexes(table)
            ),
            sorted(
                (tuple(fk["constrained_columns"]), fk["referred_table"], tuple(fk["referred_columns"]))
                for fk in inspector.get_foreign_keys(table)
            ),
        )
    return shape


def _rows(sync_url):
    engine = create_engine(sync_url)
    tables = [table for table in inspect(engine).get_table_names() if table != "alembic_version"]
    with engine.connect() as connection:
        return {
            table: sorted(map(repr, connection.execute(text(f'SELECT * FROM "{table}"')).all())) for table in tables
        }


def _seed(sync_url):
    with create_engine(sync_url).begin() as connection:
        connection.execute(
            text(
                "INSERT INTO content_filter_profiles (id, name, created_at, updated_at, categories, allow_list, "
                "block_list, strict_mode, note) VALUES (7, 'kids', '2026-09-01 00:00:00', '2026-09-01 00:00:00', "
                "'[\"adult\"]', '[]', '[\"example.com\"]', 1, 'kept across the rollback')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO users (id, username, status, used_traffic, data_limit_reset_strategy, proxy_settings, "
                "created_at, hwid_limit) VALUES (1, 'u1', 'active', 0, 'no_reset', '{}', '2026-08-08 00:00:00', 3)"
            )
        )


def _at_release_head(tmp_path, name="rollback.sqlite3"):
    config, sync_url = _database(tmp_path, name)
    upgrade(config, PROD_REVISION)
    _seed(sync_url)
    before = (_schema(sync_url), _rows(sync_url))
    upgrade(config, "heads")
    tables = set(inspect(create_engine(sync_url)).get_table_names())
    assert PASSKEY_TABLES | RELEASE_ONLY_TABLES <= tables
    assert current_heads(config) != (PROD_REVISION,)
    return config, sync_url, before


def _prod_graph_fingerprint(script, head):
    lines = []
    for revision in script.iterate_revisions(head, "base"):
        down = revision.down_revision
        down = ",".join(sorted(down)) if isinstance(down, tuple) else (down or "")
        lines.append(f"{revision.revision}<{down}")
    lines.sort()
    return len(lines), hashlib.sha256("\n".join(lines).encode()).hexdigest()


def test_the_prod_graph_is_exactly_the_lineage_of_the_prod_revision_here():
    script = ScriptDirectory.from_config(default_config())
    assert _prod_graph_fingerprint(script, PROD_REVISION) == (PROD_GRAPH_SIZE, PROD_GRAPH_FINGERPRINT)


def test_rollback_leaves_exactly_the_prod_revision_with_the_prod_schema_and_data(tmp_path):
    config, sync_url, (schema_before, rows_before) = _at_release_head(tmp_path)
    steps = []

    assert rollback(config, PROD_REVISION, echo=steps.append) == (PROD_REVISION,)

    assert _versions(sync_url) == [PROD_REVISION]
    assert _schema(sync_url) == schema_before
    assert _rows(sync_url) == rows_before
    assert "content_filter_profiles" in schema_before
    assert not (PASSKEY_TABLES | RELEASE_ONLY_TABLES) & set(_schema(sync_url))
    assert [line for line in steps if line.startswith("alembic downgrade")] == [
        f"alembic downgrade {PROD_REVISION}",
        "alembic downgrade b4c7e8f1a2d3@-1",
        "alembic downgrade 9a5c2e7f1b4d@-1",
    ]


def test_rollback_repairs_a_database_the_plain_downgrade_left_with_two_heads(tmp_path):
    config, sync_url, (schema_before, rows_before) = _at_release_head(tmp_path)
    downgrade(config, PROD_REVISION)
    assert _versions(sync_url) == ["b4c7e8f1a2d3", PROD_REVISION]

    rollback(config, PROD_REVISION, echo=lambda _line: None)

    assert _versions(sync_url) == [PROD_REVISION]
    assert _schema(sync_url) == schema_before
    assert _rows(sync_url) == rows_before


def test_rollback_is_a_no_op_at_the_prod_revision_and_refuses_below_it(tmp_path):
    config, sync_url = _database(tmp_path)
    upgrade(config, PROD_REVISION)
    rows_before = _rows(sync_url)

    assert rollback(config, PROD_REVISION, echo=lambda _line: None) == (PROD_REVISION,)
    assert _rows(sync_url) == rows_before

    downgrade(config, "d8b2c4f60a17")
    with pytest.raises(RollbackError, match="not at or above"):
        rollback(config, PROD_REVISION, echo=lambda _line: None)
    assert _versions(sync_url) == ["d8b2c4f60a17"]


def test_rollback_refuses_an_unknown_target_or_database_revision(tmp_path):
    config, sync_url = _database(tmp_path)
    upgrade(config, PROD_REVISION)
    with pytest.raises(RollbackError, match="unknown target"):
        rollback(config, "ffffffffffff", echo=lambda _line: None)
    with create_engine(sync_url).begin() as connection:
        connection.execute(text("UPDATE alembic_version SET version_num = 'ffffffffffff'"))
    with pytest.raises(RollbackError, match="does not know"):
        rollback(config, PROD_REVISION, echo=lambda _line: None)


def test_the_dry_run_changes_nothing(tmp_path, capsys):
    config, sync_url, _before = _at_release_head(tmp_path)
    heads_before = _versions(sync_url)

    assert main(["--to", PROD_REVISION, "--url", config.get_main_option("sqlalchemy.url"), "--dry-run"]) == 0

    assert _versions(sync_url) == heads_before
    printed = capsys.readouterr().out
    assert "revisions to remove: 0374ac83604b, 6698b9a695e3, 6d8eb8fba21b, 8158387867c9, 9a5c2e7f1b4d" in printed


def _prod_tree(tmp_path):
    if subprocess.run(
        ["git", "cat-file", "-e", f"{PROD_COMMIT}^{{commit}}"], cwd=ROOT, capture_output=True, check=False
    ).returncode:
        pytest.skip(f"the prod commit {PROD_COMMIT} is not in this checkout")
    tree = tmp_path / "prod"
    tree.mkdir()
    archive = subprocess.run(
        ["git", "archive", PROD_COMMIT, "app", "config.py", "role.py", "alembic.ini", "start.sh"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["tar", "-x", "-C", str(tree)], input=archive.stdout, check=True, env={**os.environ, "COPYFILE_DISABLE": "1"}
    )
    return tree


def _prod_alembic(tree, url, *arguments):
    env = {**os.environ, "SQLALCHEMY_DATABASE_URL": url}
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(tree), os.environ.get("PYTHONPATH"))))
    return subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=tree,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def test_the_prod_image_starts_on_the_rolled_back_database_and_not_on_the_plain_downgrade(tmp_path):
    tree = _prod_tree(tmp_path)
    assert "python -m alembic upgrade head" in (tree / "start.sh").read_text()
    prod_script = ScriptDirectory(str(tree / "app" / "db" / "migrations"))
    assert prod_script.get_heads() == [PROD_REVISION]
    assert _prod_graph_fingerprint(prod_script, PROD_REVISION) == (PROD_GRAPH_SIZE, PROD_GRAPH_FINGERPRINT)

    config, sync_url, _before = _at_release_head(tmp_path)
    downgrade(config, PROD_REVISION)
    refused = _prod_alembic(tree, config.get_main_option("sqlalchemy.url"), "upgrade", "head")
    assert refused.returncode != 0
    assert "Can't locate revision identified by 'b4c7e8f1a2d3'" in refused.stdout + refused.stderr

    rollback(config, PROD_REVISION, echo=lambda _line: None)
    started = _prod_alembic(tree, config.get_main_option("sqlalchemy.url"), "upgrade", "head")
    assert started.returncode == 0, started.stdout + started.stderr
    assert _versions(sync_url) == [PROD_REVISION]
