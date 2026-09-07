"""add l2tp to core type enum

Revision ID: a1c5e7d9b3f2
Revises: c3f81a7d9b42
Create Date: 2026-09-07 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "a1c5e7d9b3f2"
down_revision = "c3f81a7d9b42"
branch_labels = None
depends_on = None

VALUES_BEFORE = ("xray", "wg", "mtproto", "singbox", "openvpn")
VALUES_AFTER = (*VALUES_BEFORE, "l2tp")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.engine.name == "postgresql":
        op.execute("ALTER TYPE coretype ADD VALUE IF NOT EXISTS 'l2tp'")
    elif bind.engine.name == "sqlite":
        pass
    else:
        op.alter_column(
            "core_configs",
            "type",
            existing_type=sa.Enum(*VALUES_BEFORE, name="coretype"),
            type_=sa.Enum(*VALUES_AFTER, name="coretype"),
            existing_nullable=False,
            existing_server_default="xray",
        )


def downgrade() -> None:
    bind = op.get_bind()
    remaining = bind.execute(sa.text("SELECT COUNT(*) FROM core_configs WHERE type = 'l2tp'")).scalar()
    if remaining:
        raise RuntimeError(f"{remaining} L2TP core(s) still exist; delete them before downgrading this revision")
    if bind.engine.name not in ("postgresql", "sqlite"):
        op.alter_column(
            "core_configs",
            "type",
            existing_type=sa.Enum(*VALUES_AFTER, name="coretype"),
            type_=sa.Enum(*VALUES_BEFORE, name="coretype"),
            existing_nullable=False,
            existing_server_default="xray",
        )
