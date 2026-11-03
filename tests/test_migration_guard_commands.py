import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.command import check, current, downgrade, stamp, upgrade
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from app.fork import db_rollback, migration_guards
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
SORT_ORDER_REVISION = "d73f8a2c4e91"
REVERSE_GROUP_INDEX_REVISION = "a4d8c7e91b32"
SCRATCH_BACKEND = make_url(os.environ.get("SPLICE_SCRATCH_URL", "sqlite://")).get_backend_name()
UPGRADE_SCRIPT = (
    "import sys; from alembic.command import upgrade; from app.fork.db_rollback import default_config; "
    "upgrade(default_config(sys.argv[1]), 'head')"
)


def _record_repairs(monkeypatch) -> list:
    calls = []
    original = migration_guards.repair_interrupted_mysql_steps

    def record(*args, **kwargs):
        heads = args[2] if len(args) > 2 else kwargs["heads"]
        calls.append(tuple(sorted(heads)))
        return original(*args, **kwargs)

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


def test_a_stamp_onto_the_reverse_group_index_revision_never_reaches_the_index_hook(tmp_path, monkeypatch):
    with _scratch_database(tmp_path, "stamp_hook") as url:
        config = _config(url)
        _quiet(upgrade, config, SORT_ORDER_REVISION)
        drops = []
        monkeypatch.setattr(migration_guards, "_drop_redundant_foreign_key_indexes", drops.append)
        monkeypatch.setattr(
            migration_guards,
            "version_hooks",
            lambda _connection, _high_water: [migration_guards.MysqlReverseGroupIndexes()],
        )

        _quiet(stamp, config, REVERSE_GROUP_INDEX_REVISION)
        _quiet(stamp, config, SORT_ORDER_REVISION)
        assert drops == []

        _quiet(upgrade, config, "head")
        assert drops
        assert _versions(url) == [HEAD]


class _GetLockReturnsNull:
    dialect = SimpleNamespace(name="mysql")

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement, parameters=None):
        self.statements.append(str(statement))
        value = "panel" if "DATABASE()" in str(statement) else None
        return SimpleNamespace(scalar=lambda: value)

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


def test_a_null_from_get_lock_is_reported_as_a_lock_error_not_as_a_timeout():
    connection = _GetLockReturnsNull()
    with pytest.raises(RuntimeError) as raised, migration_guards.migration_lock(connection, timeout=1):
        pytest.fail("the migration ran without the lock")

    assert "returned NULL" in str(raised.value)
    assert not any("RELEASE_LOCK" in statement for statement in connection.statements)


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


def _migration_lock_is_free(url: str) -> bool:
    engine = create_engine(_sync(url))
    try:
        with engine.connect() as connection:
            if make_url(url).get_backend_name() == "postgresql":
                parameters = {"key": migration_guards.MIGRATION_LOCK_KEY}
                free = bool(connection.execute(text("SELECT pg_try_advisory_lock(:key)"), parameters).scalar())
                if free:
                    connection.execute(text("SELECT pg_advisory_unlock(:key)"), parameters)
                return free
            parameters = {"name": migration_guards.mysql_lock_name(connection)}
            free = connection.execute(text("SELECT GET_LOCK(:name, 0)"), parameters).scalar() == 1
            if free:
                connection.execute(text("SELECT RELEASE_LOCK(:name)"), parameters)
            return free
    finally:
        engine.dispose()


@lock_backends
def test_a_rollback_holds_the_migration_lock_across_every_downgrade_and_a_migrator_waits_for_it(tmp_path):
    with _scratch_database(tmp_path, "rollbacklock") as url:
        config = _config(url)
        _quiet(upgrade, config, "head")
        probes: list[bool] = []
        migrators: list[subprocess.Popen] = []

        def echo(line: str) -> None:
            if not line.startswith("alembic downgrade"):
                return
            probes.append(_migration_lock_is_free(url))
            if not migrators:
                migrators.append(_start_migrator(url))
                time.sleep(5)

        try:
            assert _quiet(rollback, config, PROD_REVISION, echo=echo) == (PROD_REVISION,)
            assert probes
            assert not any(probes)
            assert migrators[0].poll() is None
        finally:
            results = [_finish(migrator) for migrator in migrators]

        assert [returncode for returncode, _ in results] == [0], results
        assert _versions(url) == [HEAD]
        assert _migration_lock_is_free(url)


def _fake_sqlite_migration_lock(monkeypatch) -> None:
    monkeypatch.setitem(migration_guards.LOCKS, "sqlite", (lambda _connection, _timeout: (True, {}), "SELECT 1"))
    monkeypatch.setattr(migration_guards, "MIGRATION_LOCK_POLL", 0.01, raising=False)


def _lock_holders() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == "migration-lock-holder"]


def test_a_rollback_under_a_held_migration_lock_finishes_and_releases_it(tmp_path, monkeypatch):
    url = f"sqlite+aiosqlite:///{tmp_path / 'held_lock.sqlite3'}"
    config = _config(url)
    _quiet(upgrade, config, "head")
    _fake_sqlite_migration_lock(monkeypatch)

    assert _quiet(rollback, config, PROD_REVISION, echo=lambda _line: None) == (PROD_REVISION,)

    assert _versions(url) == [PROD_REVISION]
    assert _lock_holders() == []


def test_a_rollback_that_loses_its_migration_lock_session_stops_before_the_next_downgrade(tmp_path, monkeypatch):
    url = f"sqlite+aiosqlite:///{tmp_path / 'lost_lock.sqlite3'}"
    config = _config(url)
    _quiet(upgrade, config, "head")
    _fake_sqlite_migration_lock(monkeypatch)
    lost = threading.Event()

    def lose_the_session(_connection) -> None:
        lost.set()
        raise OperationalError("SELECT 1", {}, ConnectionResetError("lock session closed by wait_timeout"))

    original_heads = db_rollback.current_heads

    def heads_once_the_session_is_lost(config_):
        if config_.attributes.get(migration_guards.MIGRATION_LOCK_HELD):
            assert lost.wait(10)
            deadline = time.monotonic() + 10
            while _lock_holders() and time.monotonic() < deadline:
                time.sleep(0.05)
        return original_heads(config_)

    monkeypatch.setattr(migration_guards, "MIGRATION_LOCK_KEEPALIVE", 0, raising=False)
    monkeypatch.setattr(migration_guards, "_keep_lock_session_alive", lose_the_session, raising=False)
    monkeypatch.setattr(db_rollback, "current_heads", heads_once_the_session_is_lost)
    lines: list[str] = []

    with pytest.raises(migration_guards.MigrationLockError, match="lost the database migration lock"):
        _quiet(rollback, config, PROD_REVISION, echo=lines.append)

    assert not [line for line in lines if line.startswith("alembic downgrade")]
    assert _versions(url) == [HEAD]
    assert _lock_holders() == []
