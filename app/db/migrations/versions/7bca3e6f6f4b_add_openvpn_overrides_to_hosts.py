"""add openvpn_overrides to hosts

Revision ID: 7bca3e6f6f4b
Revises: f896a03c5bfb
Create Date: 2026-07-31 00:59:05.687777

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '7bca3e6f6f4b'
down_revision = 'f896a03c5bfb'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('hosts', sa.Column('openvpn_overrides', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('hosts', 'openvpn_overrides')
