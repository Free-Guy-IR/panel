from alembic.operations import Operations
from sqlalchemy.engine import Connection

ID_GUARDED_TABLES = ("users", "nodes")
AUTOINCREMENT_OWNER = "fd192e9628a5"


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
