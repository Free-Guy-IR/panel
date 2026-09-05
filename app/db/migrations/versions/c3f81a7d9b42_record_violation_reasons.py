"""record on a violation why the limiter acted

Revision ID: c3f81a7d9b42
Revises: 4d1a67aee27f
Create Date: 2026-09-05 12:00:00.000000

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision = "c3f81a7d9b42"
down_revision = "4d1a67aee27f"
branch_labels = None
depends_on = None

JSON_TYPE = sa.JSON().with_variant(JSONB(none_as_null=True), "postgresql")


def upgrade() -> None:
    with op.batch_alter_table("connection_restrictions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("reasons", JSON_TYPE, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("connection_restrictions", schema=None) as batch_op:
        batch_op.drop_column("reasons")
