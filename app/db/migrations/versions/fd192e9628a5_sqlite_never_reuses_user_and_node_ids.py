from alembic import op

revision = "fd192e9628a5"
down_revision = "009f2a24cc7b"
branch_labels = None
depends_on = None

HIGH_WATER_EVIDENCE = {
    "users": ("SELECT max(id) FROM users", "SELECT max(user_id) FROM pending_node_removals"),
    "nodes": ("SELECT max(id) FROM nodes",),
}


def _rebuild(table: str, autoincrement: bool) -> None:
    with op.batch_alter_table(table, recreate="always", table_kwargs={"sqlite_autoincrement": autoincrement}):
        pass


def _seed_high_water(connection, table: str) -> None:
    values = [connection.exec_driver_sql(query).scalar() for query in HIGH_WATER_EVIDENCE[table]]
    high = max((value for value in values if value is not None), default=0)
    connection.exec_driver_sql("DELETE FROM sqlite_sequence WHERE name = ?", (table,))
    connection.exec_driver_sql("INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)", (table, high))


def upgrade() -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    for table in HIGH_WATER_EVIDENCE:
        _rebuild(table, True)
        _seed_high_water(op.get_bind(), table)


def downgrade() -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    for table in HIGH_WATER_EVIDENCE:
        _rebuild(table, False)
