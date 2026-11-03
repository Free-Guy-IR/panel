import asyncio
import contextlib
import hashlib
import threading
import time
from collections.abc import Callable, Iterable, Iterator

from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext, RevisionStep
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, pool, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

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
REVERSE_GROUP_INDEX_COLUMNS = {
    "ix_inbounds_groups_association_group_id_inbound_id": ("group_id", "inbound_id"),
    "ix_users_groups_association_groups_id_user_id": ("groups_id", "user_id"),
}
REVISION_OBJECTS = (
    ("9a5c2e7f1b4d", ("admin_passkeys", "passkey_challenges"), ()),
    ("e5a9c3f1b7d2", ("named_locks",), ()),
    ("b7e2c4d91f60", (), (("users", "idx_users_created_at_id", ("created_at", "id")),)),
    (
        "86f7c2a14d3e",
        (),
        (("user_subscription_updates", "idx_user_subscription_updates_created_at", ("created_at",)),),
    ),
    ("b176d23e434c", ("pending_user_resyncs",), ()),
    (
        "6d8eb8fba21b",
        ("node_stat_acks",),
        (("node_stat_acks", "ix_node_stat_acks_node_id_stat_type", ("node_id", "stat_type")),),
    ),
    (
        "009f2a24cc7b",
        ("pending_node_removals",),
        (("pending_node_removals", "ix_pending_node_removals_requested_at", ("requested_at",)),),
    ),
    (
        "e79833e2a0b2",
        ("proxy_secret_claims",),
        (("proxy_secret_claims", "ix_proxy_secret_claims_user_id", ("user_id",)),),
    ),
)
MIGRATION_COMMANDS = ("upgrade", "downgrade")
MIGRATION_LOCK = "pasarguard_alembic_"
MIGRATION_LOCK_KEY = 5_026_101_103
MIGRATION_LOCK_TIMEOUT = 3600
MIGRATION_LOCK_HELD = "fork_migration_lock_held"
MIGRATION_LOCK_POLL = 0.5
MIGRATION_LOCK_KEEPALIVE = 30
MIGRATION_LOCK_RELEASE_WAIT = 60


class MigrationLockError(RuntimeError):
    pass


class MigrationLockTimeout(MigrationLockError):
    pass


class StrayMigrationObjectError(RuntimeError):
    pass


def migration_command(fn) -> str | None:
    if getattr(fn, "__module__", None) != "alembic.command":
        return None
    name = getattr(fn, "__name__", None)
    return name if name in MIGRATION_COMMANDS else None


def mysql_lock_name(connection: Connection) -> str:
    database = connection.execute(text("SELECT DATABASE()")).scalar() or ""
    return MIGRATION_LOCK + hashlib.sha1(database.encode()).hexdigest()


def _acquire_mysql_lock(connection: Connection, timeout: int) -> tuple[bool, dict]:
    name = mysql_lock_name(connection)
    acquired = connection.execute(text("SELECT GET_LOCK(:name, :timeout)"), {"name": name, "timeout": timeout}).scalar()
    if acquired is None:
        raise MigrationLockError(f"GET_LOCK('{name}') returned NULL: the server failed to take the migration lock")
    return acquired == 1, {"name": name}


def _acquire_postgresql_lock(connection: Connection, timeout: int) -> tuple[bool, dict]:
    deadline = time.monotonic() + timeout
    parameters = {"key": MIGRATION_LOCK_KEY}
    while True:
        acquired = bool(connection.execute(text("SELECT pg_try_advisory_lock(:key)"), parameters).scalar())
        connection.rollback()
        if acquired or time.monotonic() >= deadline:
            return acquired, parameters
        time.sleep(1)


LOCKS = {
    "mysql": (_acquire_mysql_lock, "SELECT RELEASE_LOCK(:name)"),
    "mariadb": (_acquire_mysql_lock, "SELECT RELEASE_LOCK(:name)"),
    "postgresql": (_acquire_postgresql_lock, "SELECT pg_advisory_unlock(:key)"),
}


def _take_lock(connection: Connection, timeout: int) -> dict | None:
    lock = LOCKS.get(connection.dialect.name)
    if lock is None:
        return None
    acquire, _release = lock
    acquired, parameters = acquire(connection, timeout)
    connection.commit()
    if not acquired:
        raise MigrationLockTimeout(f"another migration held the database migration lock for {timeout} seconds")
    return parameters


def _release_lock(connection: Connection, parameters: dict | None) -> None:
    if parameters is None:
        return
    _acquire, release = LOCKS[connection.dialect.name]
    with contextlib.suppress(SQLAlchemyError):
        connection.rollback()
        connection.execute(text(release), parameters)
        connection.commit()


@contextlib.contextmanager
def migration_lock(connection: Connection, timeout: int = MIGRATION_LOCK_TIMEOUT) -> Iterator[None]:
    parameters = _take_lock(connection, timeout)
    try:
        yield
    finally:
        _release_lock(connection, parameters)


def _keep_lock_session_alive(connection: Connection) -> None:
    connection.execute(text("SELECT 1"))
    connection.rollback()


class _MigrationLockHolder(threading.Thread):
    def __init__(self, url: str, timeout: int) -> None:
        super().__init__(name="migration-lock-holder", daemon=True)
        self.url = url
        self.timeout = timeout
        self.taken = threading.Event()
        self.finished = threading.Event()
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            asyncio.run(self._hold())
        except BaseException as exc:
            self.error = exc
        finally:
            self.taken.set()

    async def _hold(self) -> None:
        engine = create_async_engine(self.url, poolclass=pool.NullPool)
        try:
            async with engine.connect() as connection:
                parameters = await connection.run_sync(_take_lock, self.timeout)
                self.taken.set()
                try:
                    await self._keep_alive(connection)
                finally:
                    await connection.run_sync(_release_lock, parameters)
        finally:
            await engine.dispose()

    async def _keep_alive(self, connection) -> None:
        checked = time.monotonic()
        while not self.finished.is_set():
            await asyncio.sleep(MIGRATION_LOCK_POLL)
            if time.monotonic() - checked >= MIGRATION_LOCK_KEEPALIVE:
                await connection.run_sync(_keep_lock_session_alive)
                checked = time.monotonic()

    def check(self) -> None:
        if self.error is not None:
            raise MigrationLockError(
                f"the rollback lost the database migration lock ({self.error!r}); another migrator may have run, "
                "read alembic_version before going on"
            ) from self.error


@contextlib.contextmanager
def held_migration_lock(url: str, timeout: int = MIGRATION_LOCK_TIMEOUT) -> Iterator[Callable[[], None]]:
    url = migration_url(url)
    if make_url(url).get_backend_name() not in LOCKS:
        yield lambda: None
        return
    holder = _MigrationLockHolder(url, timeout)
    try:
        holder.start()
        holder.taken.wait()
        holder.check()
        yield holder.check
    finally:
        holder.finished.set()
        if holder.is_alive():
            holder.join(MIGRATION_LOCK_RELEASE_WAIT)
    holder.check()


def guard_planned_steps(migration_context: MigrationContext, script: ScriptDirectory, *, commit: bool) -> None:
    plan: Callable = migration_context._migrations_fn
    connection = migration_context.connection

    def planned(heads, context) -> list:
        steps = list(plan(heads, context))
        repair_interrupted_mysql_steps(connection, script, heads, steps)
        if commit and connection.in_transaction():
            connection.commit()
        return steps

    migration_context._migrations_fn = planned


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


def _removed(steps: Iterable) -> set[str]:
    return {step.revision.revision for step in steps if isinstance(step, RevisionStep) and step.is_downgrade}


def repair_interrupted_mysql_steps(
    connection: Connection, script: ScriptDirectory, heads: Iterable[str], steps: Iterable = ()
) -> None:
    if connection.dialect.name not in MYSQL_FAMILY:
        return
    applied = _applied(script, heads)
    _remove_unapplied_objects(connection, applied)
    _restore_applied_objects(connection, script, applied, _removed(steps))
    _reconcile_sort_order(connection, SORT_ORDER_REVISION in applied)
    if REVERSE_GROUP_INDEX_REVISION not in applied:
        _undo_reverse_group_indexes(connection)
        return
    _restore_reverse_group_indexes(connection)
    if REVERSE_GROUP_INDEX_REVISION in _removed(steps):
        _index_reverse_group_foreign_keys(connection)
    else:
        _drop_redundant_foreign_key_indexes(connection)


class MysqlReverseGroupIndexes:
    def __call__(self, *, ctx, step, heads, run_args) -> None:
        if step.is_stamp:
            return
        if step.up_revision_id == REVERSE_GROUP_INDEX_REVISION and step.is_upgrade:
            _drop_redundant_foreign_key_indexes(ctx.connection)


def version_hooks(connection: Connection, sqlite_high_water: SqliteIdHighWater | None) -> list:
    if sqlite_high_water is not None:
        return [sqlite_high_water]
    if connection.dialect.name in MYSQL_FAMILY:
        return [MysqlReverseGroupIndexes()]
    return []


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


def _foreign_key_index_name(inspector, table: str, column: str) -> str:
    names = [key["name"] for key in inspector.get_foreign_keys(table) if key["constrained_columns"] == [column]]
    return names[0] if names and names[0] else column


def _index_names(inspector, table: str) -> dict[str, list[str]]:
    return {index["name"]: index["column_names"] for index in inspector.get_indexes(table)}


def _ensure_foreign_key_index(connection: Connection, inspector, table: str, column: str, upstream_index: str) -> None:
    leading = {name for name, columns in _index_names(inspector, table).items() if columns[:1] == [column]}
    if leading - {upstream_index}:
        return
    quote = connection.dialect.identifier_preparer.quote
    name = _foreign_key_index_name(inspector, table, column)
    connection.exec_driver_sql(f"CREATE INDEX {quote(name)} ON {table} ({quote(column)})")


def _index_reverse_group_foreign_keys(connection: Connection) -> None:
    inspector = inspect(connection)
    for table, column, upstream_index in REVERSE_GROUP_FOREIGN_KEYS:
        if inspector.has_table(table):
            _ensure_foreign_key_index(connection, inspector, table, column, upstream_index)


def _restore_reverse_group_indexes(connection: Connection) -> None:
    inspector = inspect(connection)
    quote = connection.dialect.identifier_preparer.quote
    for table, _column, upstream_index in REVERSE_GROUP_FOREIGN_KEYS:
        if not inspector.has_table(table) or upstream_index in _index_names(inspector, table):
            continue
        columns = ", ".join(quote(column) for column in REVERSE_GROUP_INDEX_COLUMNS[upstream_index])
        connection.exec_driver_sql(f"CREATE INDEX {quote(upstream_index)} ON {table} ({columns})")


def _undo_reverse_group_indexes(connection: Connection) -> None:
    inspector = inspect(connection)
    quote = connection.dialect.identifier_preparer.quote
    for table, column, upstream_index in REVERSE_GROUP_FOREIGN_KEYS:
        if not inspector.has_table(table) or upstream_index not in _index_names(inspector, table):
            continue
        _ensure_foreign_key_index(connection, inspector, table, column, upstream_index)
        connection.exec_driver_sql(f"DROP INDEX {quote(upstream_index)} ON {table}")


def _drop_redundant_foreign_key_indexes(connection: Connection) -> None:
    inspector = inspect(connection)
    quote = connection.dialect.identifier_preparer.quote
    for table, column, upstream_index in REVERSE_GROUP_FOREIGN_KEYS:
        if not inspector.has_table(table):
            continue
        indexes = _index_names(inspector, table)
        name = _foreign_key_index_name(inspector, table, column)
        if upstream_index in indexes and name != upstream_index and indexes.get(name) == [column]:
            connection.exec_driver_sql(f"DROP INDEX {quote(name)} ON {table}")


def _stray_tables(inspector, applied: set[str]) -> list[tuple[str, str]]:
    return [
        (revision, table)
        for revision, tables, _indexes in reversed(REVISION_OBJECTS)
        if revision not in applied
        for table in reversed(tables)
        if inspector.has_table(table)
    ]


def _refuse_filled_stray_tables(connection: Connection, strays: list[tuple[str, str]]) -> None:
    quote = connection.dialect.identifier_preparer.quote
    filled = []
    for revision, table in strays:
        rows = connection.exec_driver_sql(f"SELECT COUNT(*) FROM {quote(table)}").scalar()
        if rows:
            filled.append(f"table {table} holds {rows} rows although revision {revision} is not applied")
    if filled:
        raise StrayMigrationObjectError(
            "; ".join(filled) + "; nothing was changed, move those rows out and drop the table by hand, then retry"
        )


def _remove_unapplied_objects(connection: Connection, applied: set[str]) -> None:
    inspector = inspect(connection)
    strays = _stray_tables(inspector, applied)
    _refuse_filled_stray_tables(connection, strays)
    quote = connection.dialect.identifier_preparer.quote
    for revision, tables, indexes in reversed(REVISION_OBJECTS):
        if revision in applied:
            continue
        for table, name, _columns in indexes:
            if table not in tables and inspector.has_table(table) and name in _index_names(inspector, table):
                connection.exec_driver_sql(f"DROP INDEX {quote(name)} ON {quote(table)}")
    for _revision, table in strays:
        connection.exec_driver_sql(f"DROP TABLE {quote(table)}")


def _seed_named_locks(connection: Connection, table, module) -> None:
    connection.execute(table.insert(), [{"name": name, "acquired_at": None} for name in module.SEEDED_LOCKS])


def _backfill_proxy_secret_claims(connection: Connection, _table, module) -> None:
    module._backfill(connection)


TABLE_REFILLS = {"named_locks": _seed_named_locks, "proxy_secret_claims": _backfill_proxy_secret_claims}


def _restore_applied_objects(
    connection: Connection, script: ScriptDirectory, applied: set[str], removed: set[str]
) -> None:
    from app.db.models import Base

    inspector = inspect(connection)
    for revision, tables, _indexes in REVISION_OBJECTS:
        if revision not in applied:
            continue
        for table in tables:
            if inspector.has_table(table):
                continue
            model = Base.metadata.tables[table]
            model.create(connection)
            refill = TABLE_REFILLS.get(table)
            if refill is not None and revision not in removed:
                refill(connection, model, script.get_revision(revision).module)
    inspector = inspect(connection)
    quote = connection.dialect.identifier_preparer.quote
    for revision, _tables, indexes in REVISION_OBJECTS:
        if revision not in applied:
            continue
        for table, name, columns in indexes:
            if inspector.has_table(table) and name not in _index_names(inspector, table):
                listed = ", ".join(quote(column) for column in columns)
                connection.exec_driver_sql(f"CREATE INDEX {quote(name)} ON {quote(table)} ({listed})")
