import sqlalchemy as sa
from alembic import op

from app.db.compiles_types import SqliteCompatibleBigInteger

revision = "b176d23e434c"
down_revision = "e5a9c3f1b7d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pending_user_resyncs",
        sa.Column("user_id", SqliteCompatibleBigInteger(), autoincrement=False, nullable=False),
        sa.Column("token", sa.String(length=32), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_pending_user_resyncs_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("user_id", name="pk_pending_user_resyncs"),
    )


def downgrade() -> None:
    op.drop_table("pending_user_resyncs")
