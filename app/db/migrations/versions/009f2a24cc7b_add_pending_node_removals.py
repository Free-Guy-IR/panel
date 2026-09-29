import sqlalchemy as sa
from alembic import op

from app.db.compiles_types import SqliteCompatibleBigInteger

revision = "009f2a24cc7b"
down_revision = "8158387867c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pending_node_removals",
        sa.Column("user_id", SqliteCompatibleBigInteger(), autoincrement=False, nullable=False),
        sa.Column("token", sa.String(length=32), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("user_id", name="pk_pending_node_removals"),
    )
    op.create_index("ix_pending_node_removals_requested_at", "pending_node_removals", ["requested_at"])


def downgrade() -> None:
    op.drop_index("ix_pending_node_removals_requested_at", table_name="pending_node_removals")
    op.drop_table("pending_node_removals")
