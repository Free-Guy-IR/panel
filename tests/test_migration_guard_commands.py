import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from alembic.command import check, current, downgrade, stamp, upgrade
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.fork import migration_guards
from app.fork.db_rollback import current_heads, rollback
from tests.test_upstream_v551_migration_splice import (
    FORK_HEAD,
    HEAD,
    PROD_REVISION,
    _config,
    _quiet,
    _scratch_database,
    _sync,
    _versions,
)

ROOT = Path(__file__).resolve().parents[1]
SCRATCH_BACKEND = make_url(os.environ.get("SPLICE_SCRATCH_URL", "sqlite://")).get_backend_name()
UPGRADE_SCRIPT = (
    "import sys; from alembic.command import upgrade; from app.fork.db_rollback import default_config; "
    "upgrade(default_config(sys.argv[1]), 'head')"
)


def _record_repairs(monkeypatch) -> list:
    calls = []
    original = migration_guards.repair_interrupted_mysql_steps

    def record(connection, script, heads, steps=()):
        calls.append(tuple(sorted(heads)))
        return original(connection, script, heads, steps)

    monkeypatch.setattr(migration_guards, "repair_interrupted_mysql_steps", record)
    return calls


def test_read_only_commands_never_reach_the_repairs(tmp_path, monkeypatch):
    with _scratch_database(tmp_path, "readonly") as url:
        config = _config(url)
        calls = _record_repairs(monkeypatch)

        _quiet(upgrade, config, "head")
        assert len(calls) == 1

        _quiet(current, config)
        _quiet(check, config)
        assert current_heads(config) == (HEAD,)
        _quiet(rollback, config, FORK_HEAD, dry_run=True, echo=lambda _line: None)
        _quiet(stamp, config, HEAD)
        assert len(calls) == 1

        _quiet(rollback, config, FORK_HEAD, echo=lambda _line: None)
        assert calls[1] == (HEAD,)
        assert current_heads(config) == (FORK_HEAD,)


def test_a_downgrade_and_an_upgrade_with_nothing_to_do_still_reach_the_repairs(tmp_path, monkeypatch):
    with _scratch_database(tmp_path, "noop") as url:
        config = _config(url)
        _quiet(upgrade, config, PROD_REVISION)
        calls = _record_repairs(monkeypatch)

        _quiet(upgrade, config, PROD_REVISION)
        _quiet(downgrade, config, PROD_REVISION)

        assert calls == [(PROD_REVISION,), (PROD_REVISION,)]


lock_backends = pytest.mark.skipif(
    SCRATCH_BACKEND not in ("mysql", "mariadb", "postgresql"),
    reason="only MySQL, MariaDB and PostgreSQL servers are shared by several migrators",
)


def _hold_migration_lock(url: str):
    engine = create_engine(_sync(url))
    connection = engine.connect()
    if make_url(url).get_backend_name() == "postgresql":
        connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": migration_guards.MIGRATION_LOCK_KEY})
    else:
        name = migration_guards.mysql_lock_name(connection)
        acquired = connection.execute(text("SELECT GET_LOCK(:name, 0)"), {"name": name}).scalar()
        assert acquired == 1
    connection.commit()
    return engine, connection


def _start_migrator(url: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", UPGRADE_SCRIPT, url],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _finish(process: subprocess.Popen) -> tuple[int, str]:
    output, _ = process.communicate(timeout=600)
    return process.returncode, output


@lock_backends
def test_a_migrator_waits_while_another_one_holds_the_migration_lock(tmp_path):
    with _scratch_database(tmp_path, "lockwait") as url:
        _quiet(upgrade, _config(url), FORK_HEAD)
        engine, holder = _hold_migration_lock(url)
        try:
            migrator = _start_migrator(url)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and migrator.poll() is None:
                time.sleep(0.5)
            assert migrator.poll() is None
            assert _versions(url) == [FORK_HEAD]
        finally:
            holder.close()
            engine.dispose()

        returncode, output = _finish(migrator)
        assert returncode == 0, output
        assert _versions(url) == [HEAD]


@lock_backends
def test_two_migrators_started_together_both_finish_at_the_head(tmp_path):
    with _scratch_database(tmp_path, "lockrace") as url:
        _quiet(upgrade, _config(url), FORK_HEAD)

        migrators = [_start_migrator(url) for _ in range(2)]
        results = [_finish(migrator) for migrator in migrators]

        assert [returncode for returncode, _ in results] == [0, 0], results
        assert _versions(url) == [HEAD]
        _quiet(check, _config(url))
