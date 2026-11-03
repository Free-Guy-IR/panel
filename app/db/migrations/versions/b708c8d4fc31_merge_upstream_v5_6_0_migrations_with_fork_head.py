from alembic import context

revision = "b708c8d4fc31"
down_revision = ("2e045b99a67e", "1a2b3c4d5e6f")
branch_labels = None
depends_on = None

ROLLBACK_FLAG = "fork_db_rollback"


def upgrade() -> None:
    pass


def downgrade() -> None:
    if not context.config.attributes.get(ROLLBACK_FLAG):
        raise RuntimeError(
            "Refusing to downgrade b708c8d4fc31 on its own: alembic would leave the upstream v5.6.0 branch "
            "(1a2b3c4d5e6f) in alembic_version, and the previous release cannot start on that. "
            "Run python -m app.fork.db_rollback --to <revision> instead; it also repairs a database this "
            "refusal stopped at b708c8d4fc31."
        )
