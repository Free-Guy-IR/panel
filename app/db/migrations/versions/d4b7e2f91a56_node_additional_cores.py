"""add additional core config ids to nodes

Revision ID: d4b7e2f91a56
Revises: a1c5e7d9b3f2
Create Date: 2026-09-10 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "d4b7e2f91a56"
down_revision = "a1c5e7d9b3f2"
branch_labels = None
depends_on = None

JSON_TYPE = sa.JSON(none_as_null=True)


def upgrade() -> None:
    op.add_column("nodes", sa.Column("additional_core_config_ids", JSON_TYPE, nullable=True))


def downgrade() -> None:
    op.drop_column("nodes", "additional_core_config_ids")
