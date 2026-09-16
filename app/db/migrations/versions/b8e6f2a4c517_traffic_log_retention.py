import sqlalchemy as sa
from alembic import op

revision = "b8e6f2a4c517"
down_revision = "a7d5e1f3b294"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "traffic_log_state",
        sa.Column("retention_hours", sa.Integer(), server_default="48", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("traffic_log_state", "retention_hours")
