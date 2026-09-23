from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5a9c3f1b7d2"
down_revision: str | Sequence[str] | None = "d8b2c4f60a17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEEDED_LOCKS = ("proxy_secrets",)


def upgrade() -> None:
    named_locks = op.create_table(
        "named_locks",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("name", name="pk_named_locks"),
    )
    op.bulk_insert(named_locks, [{"name": name, "acquired_at": None} for name in SEEDED_LOCKS])


def downgrade() -> None:
    op.drop_table("named_locks")
