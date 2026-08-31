"""merge upstream v5.3.0 migrations with fork migrations

Revision ID: 4d1a67aee27f
Revises: 7c4bd5128e62, a7f3c9e2b1d8
Create Date: 2026-08-31 01:05:16.606385

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '4d1a67aee27f'
down_revision = ('7c4bd5128e62', 'a7f3c9e2b1d8')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
