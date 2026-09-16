import sqlalchemy as sa
from alembic import op

from app.db.compiles_types import SqliteCompatibleBigInteger

revision = "a7d5e1f3b294"
down_revision = "f6c4d0e2a183"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "traffic_log_records",
        sa.Column("id", SqliteCompatibleBigInteger(), autoincrement=True, nullable=False),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_id", SqliteCompatibleBigInteger(), nullable=True),
        sa.Column("user_label", sa.String(length=128), nullable=True),
        sa.Column("node_id", SqliteCompatibleBigInteger(), nullable=False),
        sa.Column("inbound_tag", sa.String(length=256), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("protocol", sa.String(length=3), nullable=False),
        sa.Column("refused", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("route", sa.String(length=128), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hits", sa.Integer(), server_default="1", nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_traffic_log_records"),
    )
    op.create_index("ix_traffic_log_records_bucket_start", "traffic_log_records", ["bucket_start"])
    op.create_index("ix_traffic_log_records_user_last", "traffic_log_records", ["user_id", "last_seen"])
    op.create_index("ix_traffic_log_records_last_id", "traffic_log_records", ["last_seen", "id"])
    op.create_index("ix_traffic_log_records_node_last", "traffic_log_records", ["node_id", "last_seen"])

    op.create_table(
        "traffic_log_identities",
        sa.Column("user_id", SqliteCompatibleBigInteger(), autoincrement=False, nullable=False),
        sa.Column("username", sa.String(length=128), nullable=False),
        sa.Column("admin_id", SqliteCompatibleBigInteger(), nullable=True),
        sa.Column("deleted", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("user_id", name="pk_traffic_log_identities"),
    )

    op.create_table(
        "traffic_log_state",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_traffic_log_state"),
    )


def downgrade() -> None:
    op.drop_table("traffic_log_state")
    op.drop_table("traffic_log_identities")
    op.drop_index("ix_traffic_log_records_node_last", table_name="traffic_log_records")
    op.drop_index("ix_traffic_log_records_last_id", table_name="traffic_log_records")
    op.drop_index("ix_traffic_log_records_user_last", table_name="traffic_log_records")
    op.drop_index("ix_traffic_log_records_bucket_start", table_name="traffic_log_records")
    op.drop_table("traffic_log_records")
