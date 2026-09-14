"""add user subscription accesses table

Revision ID: 749cb5e672b6
Revises: 9b2d5e6f1c30
Create Date: 2026-09-14 01:04:11.436995

"""

import sqlalchemy as sa
from alembic import op

import app.db.compiles_types

revision = "749cb5e672b6"
down_revision = "9b2d5e6f1c30"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_subscription_accesses",
        sa.Column("id", app.db.compiles_types.SqliteCompatibleBigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", app.db.compiles_types.SqliteCompatibleBigInteger(), nullable=False),
        sa.Column("access_kind", sa.String(length=32), nullable=False),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_subscription_accesses_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_subscription_accesses")),
    )
    with op.batch_alter_table("user_subscription_accesses", schema=None) as batch_op:
        batch_op.create_index(
            "idx_user_subscription_accesses_user_kind_created",
            ["user_id", "access_kind", "created_at"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("user_subscription_accesses", schema=None) as batch_op:
        batch_op.drop_index("idx_user_subscription_accesses_user_kind_created")

    op.drop_table("user_subscription_accesses")
