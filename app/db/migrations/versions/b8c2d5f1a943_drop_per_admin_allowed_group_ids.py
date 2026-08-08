"""drop per-admin allowed group ids

The allowance is set on the admin's role instead, which is the mechanism the
panel already had, so the per-admin column is redundant. Dropping it does not
loosen anything: what remains is the role-level setting, and the check in
validate_all_groups that makes it apply when users are written and not only
when groups are listed.

Revision ID: b8c2d5f1a943
Revises: c4e81b70a935
Create Date: 2026-08-08 19:05:00.000000

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision = "b8c2d5f1a943"
down_revision = "c4e81b70a935"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("admins", schema=None) as batch_op:
        batch_op.drop_column("allowed_group_ids")


def downgrade() -> None:
    with op.batch_alter_table("admins", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "allowed_group_ids",
                sa.JSON().with_variant(JSONB(none_as_null=True), "postgresql"),
                nullable=True,
            )
        )
