"""keep usage history when a node is deleted

Revision ID: 3f7a9c2e1b45
Revises: e8c1f04b72a9
Create Date: 2026-09-11 22:10:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

import app.db.compiles_types

# revision identifiers, used by Alembic.
revision = "3f7a9c2e1b45"
down_revision = "e8c1f04b72a9"
branch_labels = None
depends_on = None

TABLES = ("node_usages", "node_user_usages", "node_inbound_usages", "node_usage_reset_logs")


def get_fk_name(table_name, column_names):
    """Dynamically find the foreign key name for a given table and column(s)"""
    bind = op.get_bind()
    inspector = inspect(bind)
    fks = inspector.get_foreign_keys(table_name)
    for fk in fks:
        if set(fk["constrained_columns"]) == set(column_names):
            return fk["name"]
    return None


def upgrade() -> None:
    bind = op.get_bind()
    dialect_name = bind.dialect.name

    if dialect_name == "sqlite":
        for table in TABLES:
            fk = get_fk_name(table, ["node_id"])
            with op.batch_alter_table(table, schema=None) as batch_op:
                if fk:
                    batch_op.drop_constraint(fk, type_="foreignkey")
                if table == "node_usage_reset_logs":
                    batch_op.alter_column(
                        "node_id",
                        existing_type=app.db.compiles_types.SqliteCompatibleBigInteger(),
                        nullable=True,
                    )
                batch_op.create_foreign_key(
                    f"fk_{table}_node_id_nodes", "nodes", ["node_id"], ["id"], ondelete="SET NULL"
                )
    elif dialect_name == "mysql":
        op.execute(sa.text("SET SESSION foreign_key_checks = 0"))
        for table in TABLES:
            fk = get_fk_name(table, ["node_id"])
            if fk:
                op.execute(sa.text(f"ALTER TABLE {table} DROP FOREIGN KEY {fk}"))
            if table == "node_usage_reset_logs":
                op.alter_column(
                    table,
                    "node_id",
                    existing_type=app.db.compiles_types.SqliteCompatibleBigInteger(),
                    nullable=True,
                )
            op.execute(
                sa.text(
                    f"ALTER TABLE {table} ADD CONSTRAINT fk_{table}_node_id_nodes "
                    f"FOREIGN KEY (node_id) REFERENCES nodes (id) ON DELETE SET NULL, "
                    f"ALGORITHM=INPLACE, LOCK=NONE"
                )
            )
        op.execute(sa.text("SET SESSION foreign_key_checks = 1"))
    else:
        for table in TABLES:
            fk = get_fk_name(table, ["node_id"])
            if fk:
                op.drop_constraint(fk, table, type_="foreignkey")
            if table == "node_usage_reset_logs":
                op.alter_column(
                    table,
                    "node_id",
                    existing_type=app.db.compiles_types.SqliteCompatibleBigInteger(),
                    nullable=True,
                )
            op.create_foreign_key(f"fk_{table}_node_id_nodes", table, "nodes", ["node_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    bind = op.get_bind()
    dialect_name = bind.dialect.name

    for table in TABLES:
        op.execute(sa.text(f"DELETE FROM {table} WHERE node_id IS NULL"))

    if dialect_name == "sqlite":
        for table in TABLES:
            with op.batch_alter_table(table, schema=None) as batch_op:
                batch_op.drop_constraint(f"fk_{table}_node_id_nodes", type_="foreignkey")
                if table == "node_usage_reset_logs":
                    batch_op.alter_column(
                        "node_id",
                        existing_type=app.db.compiles_types.SqliteCompatibleBigInteger(),
                        nullable=False,
                    )
                batch_op.create_foreign_key(
                    f"fk_{table}_node_id_nodes", "nodes", ["node_id"], ["id"], ondelete="CASCADE"
                )
    elif dialect_name == "mysql":
        op.execute(sa.text("SET SESSION foreign_key_checks = 0"))
        for table in TABLES:
            op.execute(sa.text(f"ALTER TABLE {table} DROP FOREIGN KEY fk_{table}_node_id_nodes"))
            if table == "node_usage_reset_logs":
                op.alter_column(
                    table,
                    "node_id",
                    existing_type=app.db.compiles_types.SqliteCompatibleBigInteger(),
                    nullable=False,
                )
            op.execute(
                sa.text(
                    f"ALTER TABLE {table} ADD CONSTRAINT fk_{table}_node_id_nodes "
                    f"FOREIGN KEY (node_id) REFERENCES nodes (id) ON DELETE CASCADE, "
                    f"ALGORITHM=INPLACE, LOCK=NONE"
                )
            )
        op.execute(sa.text("SET SESSION foreign_key_checks = 1"))
    else:
        for table in TABLES:
            op.drop_constraint(f"fk_{table}_node_id_nodes", table, type_="foreignkey")
            if table == "node_usage_reset_logs":
                op.alter_column(
                    table,
                    "node_id",
                    existing_type=app.db.compiles_types.SqliteCompatibleBigInteger(),
                    nullable=False,
                )
            op.create_foreign_key(f"fk_{table}_node_id_nodes", table, "nodes", ["node_id"], ["id"], ondelete="CASCADE")
