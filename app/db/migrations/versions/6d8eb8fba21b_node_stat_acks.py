from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.compiles_types import SqliteCompatibleBigInteger

revision: str = "6d8eb8fba21b"
down_revision: str | Sequence[str] | None = "e9a7edd9de06"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "node_stat_acks",
        sa.Column("batch_epoch", sa.String(length=64), nullable=False),
        sa.Column("stat_type", sa.SmallInteger(), nullable=False),
        sa.Column("node_id", SqliteCompatibleBigInteger(), nullable=True),
        sa.Column("batch_id", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("batch_epoch", "stat_type", name="pk_node_stat_acks"),
    )
    op.create_index("ix_node_stat_acks_node_id_stat_type", "node_stat_acks", ["node_id", "stat_type"])


def downgrade() -> None:
    op.drop_index("ix_node_stat_acks_node_id_stat_type", table_name="node_stat_acks")
    op.drop_table("node_stat_acks")
