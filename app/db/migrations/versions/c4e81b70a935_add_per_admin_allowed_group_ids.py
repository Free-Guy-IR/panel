"""add per-admin allowed group ids

Revision ID: c4e81b70a935
Revises: 92a883106837
Create Date: 2026-08-08 05:10:00.000000

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision = "c4e81b70a935"
down_revision = "92a883106837"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NULL means unrestricted, so every existing admin keeps access to every
    # group and nothing changes until the field is set deliberately.
    with op.batch_alter_table("admins", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("allowed_group_ids", sa.JSON().with_variant(JSONB(none_as_null=True), "postgresql"), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("admins", schema=None) as batch_op:
        batch_op.drop_column("allowed_group_ids")
