"""record which escalation step a restriction was

Revision ID: 7e3a91d4c8b2
Revises: 4d21e9c7b8a5
Create Date: 2026-08-09 01:10:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

import app.db.compiles_types  # noqa: F401

revision: str = "7e3a91d4c8b2"
down_revision: Union[str, None] = "4d21e9c7b8a5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "connection_restrictions",
        sa.Column("step_applied", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "connection_restrictions",
        sa.Column("disable_minutes", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("connection_restrictions", "disable_minutes")
    op.drop_column("connection_restrictions", "step_applied")
