from alembic import context

revision = "fd33c6013baa"
down_revision = ("e5a9c3f1b7d2", "b4c7e8f1a2d3")
branch_labels = None
depends_on = None

ROLLBACK_FLAG = "fork_db_rollback"


def upgrade() -> None:
    pass


def downgrade() -> None:
    if not context.config.attributes.get(ROLLBACK_FLAG):
        raise RuntimeError(
            "Refusing to downgrade fd33c6013baa on its own: alembic would leave the upstream passkey branch "
            "(b4c7e8f1a2d3) in alembic_version, and the previous release cannot start on that. "
            "Run python -m app.fork.db_rollback --to <revision> instead; it also repairs a database this "
            "refusal stopped at fd33c6013baa."
        )
