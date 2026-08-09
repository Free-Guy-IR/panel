"""store how long a user has been on two nodes at once

Revision ID: a7f3c9e2b1d8
Revises: 7e3a91d4c8b2
Create Date: 2026-08-10 01:30:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

import app.db.compiles_types  # noqa: F401

revision: str = "a7f3c9e2b1d8"
down_revision: Union[str, None] = "7e3a91d4c8b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "user_connection_states",
        sa.Column("at_once_streak", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("user_connection_states", "at_once_streak")
