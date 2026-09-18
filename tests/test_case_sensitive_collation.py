import sqlalchemy as sa
from sqlalchemy import Column, literal_column
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.dialects.mysql.mariadb import MariaDBDialect
from sqlalchemy.schema import CreateColumn

from app.db.compiles_types import CaseSensitiveString, DateDiff, DaysDiff

DIALECTS = {
    "mysql": mysql.dialect(),
    "mariadb": MariaDBDialect(),
    "postgresql": postgresql.dialect(),
    "sqlite": sqlite.dialect(),
}

EXPECTED_COLLATION = {
    "mysql": "COLLATE utf8mb4_bin",
    "mariadb": "COLLATE utf8mb4_bin",
    "postgresql": 'COLLATE "C"',
    "sqlite": "COLLATE BINARY",
}


def test_every_supported_dialect_renders_a_case_sensitive_collation():
    for name, dialect in DIALECTS.items():
        rendered = str(CreateColumn(Column("name", CaseSensitiveString(256))).compile(dialect=dialect))
        assert EXPECTED_COLLATION[name] in rendered, f"{name} rendered {rendered!r}"


def test_mariadb_renders_the_same_column_as_mysql():
    column = Column("name", CaseSensitiveString(256))
    assert str(CreateColumn(column).compile(dialect=DIALECTS["mariadb"])) == str(
        CreateColumn(column).compile(dialect=DIALECTS["mysql"])
    )


def test_the_date_helpers_compile_on_mariadb_as_they_do_on_mysql():
    for expression in (DaysDiff(literal_column("a")), DateDiff(literal_column("a"), literal_column("b"))):
        assert str(expression.compile(dialect=DIALECTS["mariadb"])) == str(
            expression.compile(dialect=DIALECTS["mysql"])
        )


def test_the_models_that_need_exact_names_use_the_case_sensitive_type():
    from app.db.models import Node, User

    assert isinstance(User.__table__.c.username.type, CaseSensitiveString)
    assert isinstance(Node.__table__.c.name.type, CaseSensitiveString)


def test_the_collation_migration_only_touches_the_mysql_family():
    from app.db.migrations.versions import d8b2c4f60a17_restore_case_sensitive_collation as migration

    assert migration.down_revision == "c7f1a9d3e604"
    assert migration.CASE_SENSITIVE == "utf8mb4_bin"
    assert {table for table, _, _ in migration.COLUMNS} == {"nodes", "users"}
    for _, _, length in migration.COLUMNS:
        assert isinstance(length, int)


def test_the_migration_leaves_other_backends_alone(monkeypatch):
    from app.db.migrations.versions import d8b2c4f60a17_restore_case_sensitive_collation as migration

    touched: list[str] = []

    class _Bind:
        dialect = sa.engine.default.DefaultDialect()

        def execute(self, *args, **kwargs):
            raise AssertionError("the migration queried information_schema on a non-mysql backend")

    monkeypatch.setattr(migration.op, "get_bind", lambda: _Bind())
    monkeypatch.setattr(migration.op, "alter_column", lambda *a, **k: touched.append(a[0]))

    migration.upgrade()
    migration.downgrade()

    assert touched == []
