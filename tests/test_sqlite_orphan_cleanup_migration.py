import importlib.util
import itertools
from pathlib import Path

import pytest
from alembic.command import upgrade
from alembic.config import Config
from sqlalchemy import create_engine, text

VERSIONS = Path(__file__).resolve().parents[1] / "app" / "db" / "migrations" / "versions"
CLEANUP_REVISION = "e9a7edd9de06"
PARENT_REVISION = "f7c63e209a37"
LOUD_RULES = {("admins", "role_id")}
TABLES_NEWER_THAN_THE_CLEANUP = {"pending_user_resyncs"}


def _cleanup_module():
    candidates = sorted(VERSIONS.glob(f"{CLEANUP_REVISION}_*.py"))
    assert len(candidates) == 1
    spec = importlib.util.spec_from_file_location("sqlite_orphan_cleanup", candidates[0])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(url, revision):
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    with create_engine(url).begin() as connection:
        config.attributes["connection"] = connection
        upgrade(config, revision)


def _revision(url):
    with create_engine(url).connect() as connection:
        return connection.execute(text("SELECT version_num FROM alembic_version")).scalar()


class Seeder:
    def __init__(self, connection):
        self.connection = connection
        self.counter = itertools.count(1)

    def columns(self, table):
        return self.connection.exec_driver_sql(f"PRAGMA table_info('{table}')").all()

    def foreign_keys(self, table):
        return {row[3]: (row[2], row[4]) for row in self.connection.exec_driver_sql(f"PRAGMA foreign_key_list('{table}')")}

    def insert(self, table, **values):
        foreign_keys = self.foreign_keys(table)
        row = {}
        for _, name, type_, notnull, default, pk in self.columns(table):
            if name in values:
                row[name] = values[name]
            elif name in foreign_keys:
                if notnull:
                    parent, key = foreign_keys[name]
                    row[name] = self.insert(parent)[key]
            elif pk and type_.upper() in ("INTEGER", "BIGINT"):
                continue
            elif "CHAR" in type_.upper() or "TEXT" in type_.upper():
                row[name] = f"v{next(self.counter)}"
            elif notnull and default is None:
                row[name] = self._filler(type_)
        names = ", ".join(row)
        placeholders = ", ".join(f":{name}" for name in row)
        rowid = self.connection.execute(text(f"INSERT INTO {table} ({names}) VALUES ({placeholders})"), row).lastrowid
        return dict(
            self.connection.execute(
                text(f"SELECT rowid AS row_ref, * FROM {table} WHERE rowid = :rowid"), {"rowid": rowid}
            ).mappings().one()
        )

    @staticmethod
    def _filler(type_):
        upper = type_.upper()
        if "DATETIME" in upper:
            return "2026-09-23 00:00:00"
        if "JSON" in upper:
            return "{}"
        if "FLOAT" in upper or "REAL" in upper:
            return 0.0
        return 0


def _schema_rules(url):
    with create_engine(url).connect() as connection:
        tables = connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'table'").scalars().all()
        rules = set()
        for table in tables:
            for row in connection.exec_driver_sql(f"PRAGMA foreign_key_list('{table}')"):
                rules.add((table, row[3], row[2], row[4]))
    return rules


def test_the_cleanup_covers_every_foreign_key_of_the_schema(tmp_path):
    module = _cleanup_module()
    url = f"sqlite:///{tmp_path / 'rules.sqlite3'}"
    _run(url, CLEANUP_REVISION)

    schema_rules = {
        rule
        for rule in _schema_rules(url)
        if (rule[0], rule[1]) not in LOUD_RULES and rule[0] not in TABLES_NEWER_THAN_THE_CLEANUP
    }
    covered = set(module.DELETE_ORPHANS) | set(module.NULL_ORPHANS)

    assert covered == schema_rules
    assert not set(module.DELETE_ORPHANS) & set(module.NULL_ORPHANS)


def test_the_cleanup_follows_each_foreign_key_rule(tmp_path):
    module = _cleanup_module()
    url = f"sqlite:///{tmp_path / 'orphans.sqlite3'}"
    _run(url, PARENT_REVISION)

    expectations = []
    gone_parents = []
    with create_engine(url).begin() as connection:
        seeder = Seeder(connection)
        for rule, action in [(rule, "delete") for rule in module.DELETE_ORPHANS] + [
            (rule, "null") for rule in module.NULL_ORPHANS
        ]:
            table, column, parent, key = rule
            live_parent = seeder.insert(parent)
            gone_parent = seeder.insert(parent)
            live_child = seeder.insert(table, **{column: live_parent[key]})
            orphan_child = seeder.insert(table, **{column: gone_parent[key]})
            gone_parents.append((parent, key, gone_parent[key]))
            expectations.append((rule, action, live_child, orphan_child, live_parent[key]))
        for parent, key, value in gone_parents:
            connection.execute(text(f"DELETE FROM {parent} WHERE {key} = :key"), {"key": value})

    _run(url, CLEANUP_REVISION)

    with create_engine(url).connect() as connection:
        for (table, column, _, _), action, live_child, orphan_child, live_key in expectations:
            def fetch(row, table=table):
                return connection.execute(
                    text(f"SELECT * FROM {table} WHERE rowid = :rowid"), {"rowid": row["row_ref"]}
                ).mappings().first()

            kept = fetch(live_child)
            assert kept is not None and kept[column] == live_key, (table, column, "live child")
            orphan = fetch(orphan_child)
            if action == "delete":
                assert orphan is None, (table, column, "orphan survived")
            else:
                assert orphan is not None and orphan[column] is None, (table, column, "dangling reference kept")
    assert _revision(url) == CLEANUP_REVISION


def test_the_cleanup_refuses_to_guess_an_admin_role_and_changes_nothing(tmp_path):
    url = f"sqlite:///{tmp_path / 'roleless.sqlite3'}"
    _run(url, PARENT_REVISION)
    with create_engine(url).begin() as connection:
        seeder = Seeder(connection)
        role = seeder.insert("admin_roles")
        seeder.insert("admins", role_id=role["id"])
        gone_user = seeder.insert("users")
        seeder.insert("user_subscription_accesses", user_id=gone_user["id"])
        connection.execute(text("DELETE FROM admin_roles WHERE id = :id"), {"id": role["id"]})
        connection.execute(text("DELETE FROM users WHERE id = :id"), {"id": gone_user["id"]})

    with pytest.raises(RuntimeError, match="admin role that no longer exists"):
        _run(url, CLEANUP_REVISION)

    assert _revision(url) == PARENT_REVISION
    with create_engine(url).connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM user_subscription_accesses")).scalar() == 1


def test_the_cleanup_leaves_a_consistent_database_untouched(tmp_path):
    url = f"sqlite:///{tmp_path / 'consistent.sqlite3'}"
    _run(url, PARENT_REVISION)
    with create_engine(url).begin() as connection:
        seeder = Seeder(connection)
        user = seeder.insert("users", admin_id=seeder.insert("admins")["id"])
        seeder.insert("user_subscription_accesses", user_id=user["id"])
        seeder.insert("node_user_usages", user_id=user["id"], node_id=seeder.insert("nodes")["id"])
        before = {
            table: connection.execute(text(f"SELECT * FROM {table} ORDER BY rowid")).all()
            for table in ("users", "admins", "user_subscription_accesses", "node_user_usages", "nodes")
        }

    _run(url, CLEANUP_REVISION)

    with create_engine(url).connect() as connection:
        for table, rows in before.items():
            assert connection.execute(text(f"SELECT * FROM {table} ORDER BY rowid")).all() == rows, table


def test_the_cleanup_does_nothing_outside_sqlite():
    module = _cleanup_module()
    executed = []

    class Bind:
        class dialect:
            name = "mysql"

        def execute(self, *args, **kwargs):
            executed.append(args)

    module.op = type("Op", (), {"get_bind": staticmethod(Bind)})
    module.upgrade()

    assert executed == []
