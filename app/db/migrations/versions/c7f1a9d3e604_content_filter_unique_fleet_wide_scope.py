"""content filter: one assignment per fleet-wide inbound

Revision ID: c7f1a9d3e604
Revises: b8e6f2a4c517
Create Date: 2026-09-17 09:40:00.000000

"""

import logging

import sqlalchemy as sa
from alembic import op

from app.db.compiles_types import SqliteCompatibleBigInteger

revision = "c7f1a9d3e604"
down_revision = "b8e6f2a4c517"
branch_labels = None
depends_on = None

TABLE = "content_filter_assignments"
COLUMN = "node_scope"
INDEX = "uq_content_filter_assignments_scope_inbound"
FLEET_WIDE = 0
CHUNK = 500

logger = logging.getLogger("alembic.runtime.migration")


def _scope(node_id, inbound_tag) -> tuple:
    return (FLEET_WIDE if node_id is None else node_id, inbound_tag)


def _prune_duplicate_scopes(bind) -> None:
    rows = bind.execute(
        sa.text(
            "SELECT id, profile_id, node_id, inbound_tag, is_enabled, enforced, applied_digest "
            f"FROM {TABLE} ORDER BY id"
        )
    ).fetchall()

    kept: dict[tuple, int] = {}
    doomed: list[tuple] = []
    for row in rows:
        key = _scope(row[2], row[3])
        if key in kept:
            doomed.append(row)
        else:
            kept[key] = row[0]

    if not doomed:
        logger.info("content filter: no duplicate assignment scopes found in %s", TABLE)
        return

    for row in doomed:
        logger.warning(
            "content filter: dropping duplicate assignment id=%s (keeping id=%s) "
            "profile_id=%s node_id=%s inbound_tag=%r is_enabled=%s enforced=%s applied_digest=%s",
            row[0],
            kept[_scope(row[2], row[3])],
            row[1],
            row[2],
            row[3],
            row[4],
            row[5],
            row[6],
        )

    statement = sa.text(f"DELETE FROM {TABLE} WHERE id IN :ids").bindparams(sa.bindparam("ids", expanding=True))
    doomed_ids = [row[0] for row in doomed]
    for start in range(0, len(doomed_ids), CHUNK):
        bind.execute(statement, {"ids": doomed_ids[start : start + CHUNK]})
    logger.warning("content filter: removed %d duplicate assignment row(s) from %s", len(doomed_ids), TABLE)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns(TABLE)}
    indexes = {index["name"] for index in inspector.get_indexes(TABLE)}

    _prune_duplicate_scopes(bind)

    if COLUMN not in columns:
        persisted = None if bind.dialect.name == "postgresql" else False
        op.add_column(
            TABLE,
            sa.Column(
                COLUMN,
                SqliteCompatibleBigInteger(),
                sa.Computed("COALESCE(node_id, 0)", persisted=persisted),
                nullable=True,
            ),
        )

    if INDEX not in indexes:
        op.create_index(INDEX, TABLE, [COLUMN, "inbound_tag"], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if INDEX in {index["name"] for index in inspector.get_indexes(TABLE)}:
        op.drop_index(INDEX, table_name=TABLE)

    if COLUMN in {column["name"] for column in inspector.get_columns(TABLE)}:
        op.drop_column(TABLE, COLUMN)
