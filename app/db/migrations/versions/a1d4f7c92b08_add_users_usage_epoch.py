"""add users usage_epoch

Revision ID: a1d4f7c92b08
Revises: 749cb5e672b6
Create Date: 2026-09-14 10:12:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "a1d4f7c92b08"
down_revision = "749cb5e672b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("usage_epoch", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
    )


def downgrade() -> None:
    op.drop_column("users", "usage_epoch")
