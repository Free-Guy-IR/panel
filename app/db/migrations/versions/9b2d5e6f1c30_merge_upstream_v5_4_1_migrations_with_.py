"""merge upstream v5.4.1 migrations with fork migrations

Revision ID: 9b2d5e6f1c30
Revises: 3f7a9c2e1b45, 48a6bcb8bba1
Create Date: 2026-09-13 11:17:33.836622

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '9b2d5e6f1c30'
down_revision = ('3f7a9c2e1b45', '48a6bcb8bba1')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
