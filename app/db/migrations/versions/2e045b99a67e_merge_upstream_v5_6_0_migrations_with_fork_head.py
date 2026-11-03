from alembic import context

revision = "2e045b99a67e"
down_revision = ("e79833e2a0b2", "1a2b3c4d5e6f")
branch_labels = None
depends_on = None

ROLLBACK_FLAG = "fork_db_rollback"


def upgrade() -> None:
    pass


def downgrade() -> None:
    if not context.config.attributes.get(ROLLBACK_FLAG):
        raise RuntimeError(
            "Refusing to downgrade 2e045b99a67e on its own: alembic would leave the upstream v5.6.0 branch "
            "(1a2b3c4d5e6f) in alembic_version, and the previous release cannot start on that. "
            "Run python -m app.fork.db_rollback --to <revision> instead; it also repairs a database this "
            "refusal stopped at 2e045b99a67e."
        )
