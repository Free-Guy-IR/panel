"""content filter: remember the sniffing a filter replaced

Revision ID: e5b3c9d1f072
Revises: d4a92bc8e531
Create Date: 2026-09-15 08:20:00.000000

"""

import sqlalchemy as sa
from alembic import op

from app.db.compiles_types import SqliteCompatibleBigInteger

revision = "e5b3c9d1f072"
down_revision = "d4a92bc8e531"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "content_filter_sniffing_overrides",
        sa.Column("id", SqliteCompatibleBigInteger(), autoincrement=True, nullable=False),
        sa.Column("core_id", SqliteCompatibleBigInteger(), nullable=False),
        sa.Column("inbound_tag", sa.String(length=256), nullable=False),
        sa.Column("original", sa.JSON(), nullable=True),
        sa.Column("installed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["core_id"],
            ["core_configs.id"],
            name="fk_content_filter_sniffing_overrides_core_id_core_configs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_content_filter_sniffing_overrides"),
        sa.UniqueConstraint("core_id", "inbound_tag", name="uq_content_filter_sniffing_overrides_core_inbound"),
    )


def downgrade() -> None:
    op.drop_table("content_filter_sniffing_overrides")
