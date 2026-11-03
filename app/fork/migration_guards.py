from collections.abc import Iterable

from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.engine import Connection, make_url

ID_GUARDED_TABLES = ("users", "nodes")
AUTOINCREMENT_OWNER = "fd192e9628a5"
MYSQL_FAMILY = ("mysql", "mariadb")
SORT_ORDER_REVISION = "d73f8a2c4e91"
SORT_ORDER_TABLES = ("nodes", "core_configs")
REVERSE_GROUP_INDEX_REVISION = "a4d8c7e91b32"
REVERSE_GROUP_FOREIGN_KEYS = (
    ("inbounds_groups_association", "group_id", "ix_inbounds_groups_association_group_id_inbound_id"),
    ("users_groups_association", "groups_id", "ix_users_groups_association_groups_id_user_id"),
)


def migration_url(url: str) -> str:
    parsed = make_url(url)
    if parsed.get_backend_name() != "mariadb":
        return url
    return parsed.set(drivername="mysql" + parsed.drivername[len("mariadb") :]).render_as_string(hide_password=False)


def _sqlite_definition(connection: Connection, name: str) -> str | None:
    return connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).scalar()


def _sqlite_sequence(connection: Connection, table: str) -> int | None:
    if _sqlite_definition(connection, "sqlite_sequence") is None:
        return None
    return connection.exec_driver_sql("SELECT seq FROM sqlite_sequence WHERE name = ?", (table,)).scalar()


def _id_high_water(connection: Connection) -> dict[str, tuple[bool, int | None]]:
    marks = {}
    for table in ID_GUARDED_TABLES:
        definition = _sqlite_definition(connection, table)
        if definition is not None:
            marks[table] = ("AUTOINCREMENT" in definition.upper(), _sqlite_sequence(connection, table))
    return marks


class SqliteIdHighWater:
    def __init__(self) -> None:
        self.marks: dict[str, tuple[bool, int | None]] = {}

    def prime(self, connection: Connection) -> None:
        self.marks = _id_high_water(connection)

    def __call__(self, *, ctx, step, heads, run_args) -> None:
        if step.up_revision_id != AUTOINCREMENT_OWNER:
            for table, (autoincrement, sequence) in self.marks.items():
                if autoincrement:
                    _keep_high_water(ctx, table, sequence)
        self.marks = _id_high_water(ctx.connection)


def _keep_high_water(ctx, table: str, sequence: int | None) -> None:
    connection = ctx.connection
    definition = _sqlite_definition(connection, table)
    if definition is None:
        return
    if "AUTOINCREMENT" not in definition.upper():
        with Operations(ctx).batch_alter_table(table, recreate="always", table_kwargs={"sqlite_autoincrement": True}):
            pass
    current = _sqlite_sequence(connection, table)
    highest = connection.exec_driver_sql(f"SELECT max(id) FROM {table}").scalar()
    mark = max((value for value in (sequence, current, highest) if value is not None), default=None)
    if mark is None or mark == current:
        return
    connection.exec_driver_sql("DELETE FROM sqlite_sequence WHERE name = ?", (table,))
    connection.exec_driver_sql("INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)", (table, mark))


def _applied(script: ScriptDirectory, heads: Iterable[str]) -> set[str]:
    applied: set[str] = set()
    for head in heads:
        applied.update(revision.revision for revision in script.iterate_revisions(head, "base"))
    return applied


def repair_interrupted_mysql_steps(
    connection: Connection, script: ScriptDirectory, heads: Iterable[str], *, rolling_back: bool
) -> None:
    if connection.dialect.name not in MYSQL_FAMILY:
        return
    applied = _applied(script, heads)
    _reconcile_sort_order(connection, SORT_ORDER_REVISION in applied)
    if rolling_back and REVERSE_GROUP_INDEX_REVISION in applied:
        _index_reverse_group_foreign_keys(connection)


def _reconcile_sort_order(connection: Connection, applied: bool) -> None:
    inspector = inspect(connection)
    for table in SORT_ORDER_TABLES:
        if not inspector.has_table(table):
            continue
        present = "sort_order" in {column["name"] for column in inspector.get_columns(table)}
        if present and not applied:
            connection.exec_driver_sql(f"ALTER TABLE {table} DROP COLUMN sort_order")
        elif applied and not present:
            connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN sort_order INTEGER NOT NULL DEFAULT '0'")
            connection.exec_driver_sql(f"UPDATE {table} SET sort_order = id")


def _index_reverse_group_foreign_keys(connection: Connection) -> None:
    inspector = inspect(connection)
    quote = connection.dialect.identifier_preparer.quote
    for table, column, upstream_index in REVERSE_GROUP_FOREIGN_KEYS:
        if not inspector.has_table(table):
            continue
        leading = {index["name"] for index in inspector.get_indexes(table) if index["column_names"][:1] == [column]}
        if leading - {upstream_index}:
            continue
        names = [key["name"] for key in inspector.get_foreign_keys(table) if key["constrained_columns"] == [column]]
        name = names[0] if names and names[0] else column
        connection.exec_driver_sql(f"CREATE INDEX {quote(name)} ON {table} ({quote(column)})")
