"""store how long a user has been on more than one node

Revision ID: 4d21e9c7b8a5
Revises: 9338a6417365
Create Date: 2026-08-09 00:20:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

import app.db.compiles_types  # noqa: F401

revision: str = "4d21e9c7b8a5"
down_revision: Union[str, None] = "9338a6417365"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "user_connection_states",
        sa.Column("node_streak", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("user_connection_states", "node_streak")
