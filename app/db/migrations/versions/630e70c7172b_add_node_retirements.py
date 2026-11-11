import sqlalchemy as sa
from alembic import context, op

from app.db.compiles_types import SqliteCompatibleBigInteger

revision = "630e70c7172b"
down_revision = "2d198a5a8068"
branch_labels = None
depends_on = None

GUARDED_MERGES = ("b708c8d4fc31", "fd33c6013baa")
ROLLBACK_FLAG = "fork_db_rollback"


def upgrade() -> None:
    op.create_table(
        "node_retirements",
        sa.Column("id", SqliteCompatibleBigInteger(), autoincrement=True, nullable=False),
        sa.Column("node_id", SqliteCompatibleBigInteger(), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("connection_type", sa.String(length=16), nullable=False),
        sa.Column("address", sa.String(length=256), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("api_port", sa.Integer(), nullable=False),
        sa.Column("server_ca", sa.String(length=2048), nullable=False),
        sa.Column("api_key", sa.String(length=36), nullable=True),
        sa.Column("proxy_url", sa.String(length=256), nullable=True),
        sa.Column("default_timeout", sa.Integer(), nullable=False),
        sa.Column("internal_timeout", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(length=1024), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_node_retirements"),
    )
    op.create_index("ix_node_retirements_next_attempt_at", "node_retirements", ["next_attempt_at"])


def _plan_removes_a_guarded_merge() -> bool:
    migration_context = context.get_context()
    plan = migration_context.opts.get("fn")
    if plan is None:
        return True
    steps = plan(migration_context.get_current_heads(), migration_context)
    return any(
        not step.is_upgrade and set(GUARDED_MERGES) & set(step.from_revisions) for step in steps
    )


def downgrade() -> None:
    if not context.config.attributes.get(ROLLBACK_FLAG) and _plan_removes_a_guarded_merge():
        raise RuntimeError(
            f"Refusing a plain downgrade from {revision} past the guarded merges {', '.join(GUARDED_MERGES)}: "
            "nothing was changed, and the owed node Stops in node_retirements are kept. "
            "Run python -m app.fork.db_rollback --to <revision> instead."
        )
    op.execute("DELETE FROM node_retirements")
    op.drop_index("ix_node_retirements_next_attempt_at", table_name="node_retirements")
    op.drop_table("node_retirements")
