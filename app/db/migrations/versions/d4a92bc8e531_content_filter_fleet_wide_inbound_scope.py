"""content filter: allow an inbound-only scope

Revision ID: d4a92bc8e531
Revises: c3f81ab7d420
Create Date: 2026-09-15 06:05:00.000000

"""

import sqlalchemy as sa
from alembic import context, op

from app.db.compiles_types import SqliteCompatibleBigInteger

revision = "d4a92bc8e531"
down_revision = "c3f81ab7d420"
branch_labels = None
depends_on = None


def data_loss_allowed() -> bool:
    return context.get_x_argument(as_dictionary=True).get("allow_data_loss", "").lower() == "true"


def upgrade() -> None:
    with op.batch_alter_table("content_filter_assignments") as batch:
        batch.alter_column("node_id", existing_type=SqliteCompatibleBigInteger(), nullable=True)


def downgrade() -> None:
    fleet_wide = op.get_bind().execute(
        sa.text("SELECT COUNT(*) FROM content_filter_assignments WHERE node_id IS NULL")
    ).scalar()
    if fleet_wide and not data_loss_allowed():
        raise RuntimeError(
            f"Downgrading d4a92bc8e531 deletes {fleet_wide} fleet-wide content-filter assignment(s); "
            "back them up and rerun with -x allow_data_loss=true to accept the loss"
        )
    if fleet_wide:
        op.execute("DELETE FROM content_filter_assignments WHERE node_id IS NULL")
    with op.batch_alter_table("content_filter_assignments") as batch:
        batch.alter_column("node_id", existing_type=SqliteCompatibleBigInteger(), nullable=False)
