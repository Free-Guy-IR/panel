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
MYSQL_FAMILY = ("mysql", "mariadb")
COLUMNS = (("nodes", "name", 256), ("users", "username", 128))


def _collation(bind, table: str, column: str) -> str | None:
    return bind.execute(
        sa.text(
            "SELECT COLLATION_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table AND COLUMN_NAME = :column"
        ),
        {"table": table, "column": column},
    ).scalar()


def _target_collisions(bind, table: str, column: str) -> list[str]:
    rows = bind.execute(
        sa.text(
            f"SELECT CONVERT(`{column}` USING utf8mb4) COLLATE {CASE_SENSITIVE} AS folded, "  # noqa: S608
            f"COUNT(*) AS hits FROM `{table}` GROUP BY folded HAVING hits > 1"
        )
    ).fetchall()
    return [str(row[0]) for row in rows]


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name not in MYSQL_FAMILY:
        return

    for table, column, length in COLUMNS:
        current = _collation(bind, table, column)
        if current is None:
            continue
        if current == CASE_SENSITIVE:
            logger.info(f"content filter: {table}.{column} is already {CASE_SENSITIVE}")
            continue

        collisions = _target_collisions(bind, table, column)
        if collisions:
            shown = ", ".join(sorted(collisions)[:5])
            raise RuntimeError(
                f"{table}.{column} already holds {len(collisions)} group(s) of rows that {CASE_SENSITIVE} would "
                f"read as the same value: {shown}. Rebuilding the unique index under that collation would fail on "
                "them, so nothing has been changed. Make those values differ, then run this migration again."
            )

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
    if op.get_bind().dialect.name not in MYSQL_FAMILY:
        return
    logger.warning(
        "content filter: leaving the column collations alone. This migration only repairs columns whose "
        "collation had drifted from the model, and it does not record which ones it touched, so stripping the "
        "collation here would also strip it from columns that were already correct before it ran."
    )
