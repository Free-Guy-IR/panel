"""merge heads: openvpn core type + admin unique constraint

Revision ID: f896a03c5bfb
Revises: 8a1f3c6e9d02, fb32155473c1
Create Date: 2026-07-30 19:11:10.019634

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f896a03c5bfb'
down_revision = ('8a1f3c6e9d02', 'fb32155473c1')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
