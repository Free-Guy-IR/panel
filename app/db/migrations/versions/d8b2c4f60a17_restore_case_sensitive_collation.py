"""content filter: restore the case-sensitive collation the model asks for

Revision ID: d8b2c4f60a17
Revises: c7f1a9d3e604
Create Date: 2026-09-18 03:50:00.000000

"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "d8b2c4f60a17"
down_revision: str | Sequence[str] | None = "c7f1a9d3e604"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

CASE_SENSITIVE = "utf8mb4_bin"
COLUMNS = (("nodes", "name", 256), ("users", "username", 128))


def _collation(bind, table: str, column: str) -> str | None:
    return bind.execute(
        sa.text(
            "SELECT COLLATION_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table AND COLUMN_NAME = :column"
        ),
        {"table": table, "column": column},
    ).scalar()


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name not in ("mysql", "mariadb"):
        return

    for table, column, length in COLUMNS:
        current = _collation(bind, table, column)
        if current is None:
            continue
        if current == CASE_SENSITIVE:
            logger.info(f"content filter: {table}.{column} is already {CASE_SENSITIVE}")
            continue

        logger.warning(
            f"content filter: {table}.{column} is {current}, so names differing only in case cannot coexist; "
            f"changing it to {CASE_SENSITIVE} to match the model"
        )
        op.alter_column(
            table,
            column,
            existing_type=sa.VARCHAR(length=length),
            type_=mysql.VARCHAR(length=length, charset="utf8mb4", collation=CASE_SENSITIVE),
            existing_nullable=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name not in ("mysql", "mariadb"):
        return

    for table, column, length in COLUMNS:
        if _collation(bind, table, column) is None:
            continue
        op.alter_column(
            table,
            column,
            existing_type=mysql.VARCHAR(length=length, charset="utf8mb4", collation=CASE_SENSITIVE),
            type_=sa.String(length=length),
            existing_nullable=False,
        )
