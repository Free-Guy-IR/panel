"""add openvpn to core type enum

Revision ID: 8a1f3c6e9d02
Revises: f976bfcf4738
Create Date: 2026-07-30 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "8a1f3c6e9d02"
down_revision = "f976bfcf4738"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.engine.name == "postgresql":
        op.execute("ALTER TYPE coretype ADD VALUE IF NOT EXISTS 'openvpn'")
    elif bind.engine.name == "sqlite":
        # SQLite has no native ENUM/ALTER COLUMN TYPE support - sa.Enum is
        # stored as an unconstrained VARCHAR there (used by the test suite),
        # so there is no DB-level constraint to update at all.
        pass
    else:
        # MySQL/others: sa.Enum renders as a native ENUM(...) column, so the
        # allowed value list must be altered explicitly - adding a Python-side
        # CoreType member alone does not change the DB constraint.
        core_type = sa.Enum("xray", "wg", "mtproto", "singbox", "openvpn", name="coretype")
        op.alter_column(
            "core_configs",
            "type",
            existing_type=sa.Enum("xray", "wg", "mtproto", "singbox", name="coretype"),
            type_=core_type,
            existing_nullable=False,
            existing_server_default="xray",
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.engine.name not in ("postgresql", "sqlite"):
        core_type = sa.Enum("xray", "wg", "mtproto", "singbox", name="coretype")
        op.alter_column(
            "core_configs",
            "type",
            existing_type=sa.Enum("xray", "wg", "mtproto", "singbox", "openvpn", name="coretype"),
            type_=core_type,
            existing_nullable=False,
            existing_server_default="xray",
        )
