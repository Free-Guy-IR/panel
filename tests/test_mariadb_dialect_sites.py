from types import SimpleNamespace

import pytest
from sqlalchemy import Column, MetaData, Table, column
from sqlalchemy.dialects.mysql import Insert as MySQLInsert
from sqlalchemy.dialects.mysql.mariadb import MariaDBDialect
from sqlalchemy.schema import CreateTable
from sqlalchemy.sql import Delete, Select

from app.db.compiles_types import WebAuthnBinary, WebAuthnChallenge, WebAuthnCredentialId
from app.db.crud import general, host, hwid, wireguard
from app.db.models import UserHWID
from app.jobs import cleanup_subscription_updates
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
async def test_subscription_update_cleanup_avoids_limit_subquery_on_mariadb(monkeypatch):
    db = _MariaDBSession(first_rows=[SimpleNamespace(user_id=7)])

    async def _no_flush():
        return None

    monkeypatch.setattr(cleanup_subscription_updates, "flush_user_sub_updates", _no_flush)
    monkeypatch.setattr(cleanup_subscription_updates, "GetDB", lambda: db)

    await cleanup_subscription_updates.cleanup_user_subscription_updates()

    assert isinstance(db.statements[1], Select)
    assert not any(isinstance(stmt, Delete) for stmt in db.statements[1:])
