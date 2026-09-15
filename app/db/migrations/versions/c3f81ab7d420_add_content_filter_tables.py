"""add content filter tables

Revision ID: c3f81ab7d420
Revises: a1d4f7c92b08
Create Date: 2026-09-15 05:10:00.000000

"""

import sqlalchemy as sa
from alembic import op

from app.db.compiles_types import SqliteCompatibleBigInteger

revision = "c3f81ab7d420"
down_revision = "a1d4f7c92b08"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "content_filter_profiles",
        sa.Column("id", SqliteCompatibleBigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("categories", sa.JSON(), nullable=True),
        sa.Column("allow_list", sa.JSON(), nullable=True),
        sa.Column("block_list", sa.JSON(), nullable=True),
        sa.Column("strict_mode", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("note", sa.String(length=256), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_content_filter_profiles"),
        sa.UniqueConstraint("name", name="uq_content_filter_profiles_name"),
    )

    op.create_table(
        "content_filter_assignments",
        sa.Column("id", SqliteCompatibleBigInteger(), autoincrement=True, nullable=False),
        sa.Column("profile_id", SqliteCompatibleBigInteger(), nullable=False),
        sa.Column("node_id", SqliteCompatibleBigInteger(), nullable=False),
        sa.Column("inbound_tag", sa.String(length=256), server_default="", nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("enforced", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("applied_digest", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["content_filter_profiles.id"],
            name="fk_content_filter_assignments_profile_id_content_filter_profiles",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["node_id"],
            ["nodes.id"],
            name="fk_content_filter_assignments_node_id_nodes",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_content_filter_assignments"),
        sa.UniqueConstraint("node_id", "inbound_tag", name="uq_content_filter_assignments_node_inbound"),
    )
    op.create_index("ix_content_filter_assignments_profile", "content_filter_assignments", ["profile_id"])
    op.create_index("ix_content_filter_assignments_node", "content_filter_assignments", ["node_id"])


def downgrade() -> None:
    op.drop_index("ix_content_filter_assignments_node", table_name="content_filter_assignments")
    op.drop_index("ix_content_filter_assignments_profile", table_name="content_filter_assignments")
    op.drop_table("content_filter_assignments")
    op.drop_table("content_filter_profiles")
