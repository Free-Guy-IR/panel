"""move node additional cores into an association table

Revision ID: e8c1f04b72a9
Revises: d4b7e2f91a56
Create Date: 2026-09-10 01:00:00.000000

"""

import json

import sqlalchemy as sa
from alembic import op

revision = "e8c1f04b72a9"
down_revision = "d4b7e2f91a56"
branch_labels = None
depends_on = None

JSON_TYPE = sa.JSON(none_as_null=True)


def _id_type(bind) -> sa.types.TypeEngine:
    return sa.BigInteger() if bind.engine.name != "sqlite" else sa.Integer()


def upgrade() -> None:
    bind = op.get_bind()
    id_type = _id_type(bind)

    op.create_table(
        "node_additional_cores",
        sa.Column("node_id", id_type, sa.ForeignKey("nodes.id", ondelete="CASCADE"), primary_key=True),
        sa.Column(
            "core_config_id", id_type, sa.ForeignKey("core_configs.id", ondelete="CASCADE"), primary_key=True
        ),
    )

    rows = bind.execute(
        sa.text("SELECT id, additional_core_config_ids FROM nodes WHERE additional_core_config_ids IS NOT NULL")
    ).fetchall()

    valid_cores = {row[0] for row in bind.execute(sa.text("SELECT id FROM core_configs")).fetchall()}

    seen = set()
    pairs = []
    for node_id, raw in rows:
        if raw is None:
            continue
        core_ids = raw if isinstance(raw, list) else json.loads(raw)
        for core_id in core_ids or []:
            if core_id in valid_cores and (node_id, core_id) not in seen:
                seen.add((node_id, core_id))
                pairs.append((node_id, core_id))

    if pairs:
        bind.execute(
            sa.text("INSERT INTO node_additional_cores (node_id, core_config_id) VALUES (:node_id, :core_config_id)"),
            [{"node_id": n, "core_config_id": c} for n, c in pairs],
        )

    op.drop_column("nodes", "additional_core_config_ids")


def downgrade() -> None:
    bind = op.get_bind()
    op.add_column("nodes", sa.Column("additional_core_config_ids", JSON_TYPE, nullable=True))

    rows = bind.execute(
        sa.text("SELECT node_id, core_config_id FROM node_additional_cores ORDER BY node_id, core_config_id")
    ).fetchall()

    by_node: dict[int, list[int]] = {}
    for node_id, core_id in rows:
        ids = by_node.setdefault(node_id, [])
        if core_id not in ids:
            ids.append(core_id)

    for node_id, core_ids in by_node.items():
        bind.execute(
            sa.text("UPDATE nodes SET additional_core_config_ids = :ids WHERE id = :node_id"),
            {"ids": json.dumps(core_ids), "node_id": node_id},
        )

    op.drop_table("node_additional_cores")
