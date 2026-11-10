import ast
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import Column, MetaData, Table, column
from sqlalchemy.dialects import mysql
from sqlalchemy.dialects.mysql import Insert as MySQLInsert
from sqlalchemy.dialects.mysql.mariadb import MariaDBDialect
from sqlalchemy.schema import CreateTable
from sqlalchemy.sql import Delete, Select

from app.db.compiles_types import DateDiff, WebAuthnBinary, WebAuthnChallenge, WebAuthnCredentialId
from app.db.crud import general, host, hwid, wireguard
from app.db.models import UserHWID
from app.jobs import cleanup_subscription_updates
from app.models.settings import Subscription
from app.models.stats import Period


class _Result:
    def __init__(self, rows=()):
        self._rows = list(rows)
        self.rowcount = 0

    def __iter__(self):
        return iter(self._rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _MariaDBSession:
    def __init__(self, first_rows=()):
        self.bind = SimpleNamespace(dialect=SimpleNamespace(name="mariadb"))
        self.statements = []
        self._first_rows = list(first_rows)

    async def execute(self, stmt, params=None):
        self.statements.append(stmt)
        if len(self.statements) == 1 and self._first_rows:
            return _Result(self._first_rows)
        return _Result()

    async def flush(self):
        return None

    async def commit(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _mariadb_column_ddl(type_):
    table = Table("t", MetaData(), Column("value", type_))
    return str(CreateTable(table).compile(dialect=MariaDBDialect())).split("(", 1)[1].strip()


@pytest.mark.parametrize(
    "type_, expected",
    [
        (WebAuthnBinary(64), "TINYBLOB"),
        (WebAuthnBinary(1024), "BLOB"),
        (WebAuthnCredentialId(1024), "VARBINARY(1024)"),
        (WebAuthnChallenge(128), "VARBINARY(128)"),
    ],
)
def test_webauthn_columns_use_the_mysql_types_on_mariadb(type_, expected):
    ddl = _mariadb_column_ddl(type_)
    assert ddl.splitlines()[0] == f"value {expected}"


@pytest.mark.asyncio
async def test_upsert_inbounds_takes_the_mysql_upsert_on_mariadb():
    db = _MariaDBSession()
    await host.upsert_inbounds(db, ["vless-in"])
    assert isinstance(db.statements[0], MySQLInsert)


@pytest.mark.asyncio
async def test_register_user_hwid_takes_the_mysql_upsert_on_mariadb():
    db = _MariaDBSession()
    await hwid.register_user_hwid(db, 1, "hw-1", "android", "14", "pixel")
    assert len(db.statements) == 1
    assert isinstance(db.statements[0], MySQLInsert)
    assert db.statements[0].table is UserHWID.__table__


@pytest.mark.asyncio
async def test_wireguard_subnet_rows_take_the_mysql_upsert_on_mariadb():
    db = _MariaDBSession()
    await wireguard._lock_subnet_rows(db, ["10.0.0.0/24"])
    assert isinstance(db.statements[0], MySQLInsert)
    assert db.statements[1]._for_update_arg is not None


def test_datetime_add_expression_works_on_mariadb():
    db = _MariaDBSession()
    expr = general.get_datetime_add_expression(db, column("created_at"), 30)
    assert "date_add" in str(expr).lower()


@pytest.mark.parametrize("dialect", [mysql.dialect(), MariaDBDialect()])
def test_reset_cycle_date_diff_counts_elapsed_seconds(dialect):
    compiled = str(DateDiff(column("online_at"), column("created_at")).compile(dialect=dialect))
    assert compiled == "TIMESTAMPDIFF(SECOND, created_at, online_at) / 86400"
    assert "DATEDIFF" not in compiled


def test_json_extract_works_on_mariadb():
    db = _MariaDBSession()
    expr = general.json_extract(db, column("proxy_settings"), "$.vless.id")
    assert expr is not None
    assert "json_unquote" in str(expr).lower()


def test_period_truncation_works_on_mariadb():
    db = _MariaDBSession()
    expr = general._build_trunc_expression(db, Period.day, column("created_at"))
    assert "date_format" in str(expr).lower()


@pytest.mark.asyncio
async def test_subscription_update_cleanup_ranks_in_one_delete_on_mariadb(monkeypatch):
    db = _MariaDBSession(first_rows=[SimpleNamespace(user_id=7)])

    async def _no_flush():
        return None

    async def _default_subscription():
        return Subscription.model_construct()

    monkeypatch.setattr(cleanup_subscription_updates, "flush_user_sub_updates", _no_flush)
    monkeypatch.setattr(cleanup_subscription_updates, "subscription_settings", _default_subscription)
    monkeypatch.setattr(cleanup_subscription_updates, "GetDB", lambda: db)

    await cleanup_subscription_updates.cleanup_user_subscription_updates()

    assert len(db.statements) == 1
    assert isinstance(db.statements[0], Delete)
    assert not any(isinstance(stmt, Select) for stmt in db.statements)
    compiled = str(db.statements[0].compile(dialect=MariaDBDialect()))
    assert "row_number() OVER (PARTITION BY" in compiled
    assert "LIMIT" not in compiled


APP = Path(__file__).resolve().parents[1] / "app"
MIGRATIONS_ENV = APP / "db" / "migrations" / "env.py"
HISTORICAL_MIGRATIONS = APP / "db" / "migrations" / "versions"
RAW_MYSQL_NAME_COMPARE = re.compile(r"""\.name\s*(==|!=)\s*["']mysql["']|["']mysql["']\s*(==|!=)\s*[\w.]+\.name\b""")


def _migrations_env_compare_type():
    tree = ast.parse(MIGRATIONS_ENV.read_text())
    kept = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    kept += [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_compare_type"]
    namespace = {}
    exec(compile(ast.Module(body=kept, type_ignores=[]), str(MIGRATIONS_ENV), "exec"), namespace)  # noqa: S102
    return namespace["_compare_type"]


@pytest.mark.parametrize("dialect_name", ["mysql", "mariadb"])
@pytest.mark.parametrize(
    "inspected_type, metadata_type",
    [
        (mysql.TINYBLOB(), WebAuthnBinary(64)),
        (mysql.BLOB(), WebAuthnBinary(1024)),
        (mysql.VARBINARY(1024), WebAuthnCredentialId(1024)),
        (mysql.VARBINARY(128), WebAuthnChallenge(128)),
    ],
)
def test_migrations_env_accepts_reflected_webauthn_types_on_the_mysql_family(
    dialect_name, inspected_type, metadata_type
):
    compare_type = _migrations_env_compare_type()
    context = SimpleNamespace(dialect=SimpleNamespace(name=dialect_name))
    assert compare_type(context, None, None, inspected_type, metadata_type) is False


@pytest.mark.parametrize("dialect_name", ["mysql", "mariadb"])
def test_migrations_env_still_reports_a_resized_webauthn_column_on_the_mysql_family(dialect_name):
    compare_type = _migrations_env_compare_type()
    context = SimpleNamespace(dialect=SimpleNamespace(name=dialect_name))
    assert compare_type(context, None, None, mysql.VARBINARY(64), WebAuthnChallenge(128)) is None


def test_no_live_code_compares_a_raw_dialect_name_to_mysql():
    offenders = []
    for path in sorted(APP.rglob("*.py")):
        if HISTORICAL_MIGRATIONS in path.parents:
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if RAW_MYSQL_NAME_COMPARE.search(line):
                offenders.append(f"{path.relative_to(APP.parent)}:{lineno}: {line.strip()}")
    assert offenders == []
