import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f7c63e209a37"
down_revision = "6698b9a695e3"
branch_labels = None
depends_on = None

COLUMNS = (
    ("content_filter_profiles", "categories", True),
    ("content_filter_profiles", "allow_list", True),
    ("content_filter_profiles", "block_list", True),
    ("content_filter_sniffing_overrides", "original", True),
    ("admin_roles", "hwid", False),
    ("admins", "custom_variables", True),
    ("api_keys", "permissions", False),
)


def _column_types(bind) -> dict[tuple[str, str], str]:
    rows = bind.execute(
        sa.text(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema()"
        )
    )
    return {(table, column): data_type for table, column, data_type in rows}


def _retype(target: str, existing, new) -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    current = _column_types(bind)
    for table, column, nullable in COLUMNS:
        if current.get((table, column)) in (None, target):
            continue
        op.alter_column(
            table,
            column,
            existing_type=existing,
            type_=new,
            existing_nullable=nullable,
            postgresql_using=f"{column}::{target}",
        )


def upgrade() -> None:
    _retype("jsonb", sa.JSON(), postgresql.JSONB(none_as_null=True))


def downgrade() -> None:
    _retype("json", postgresql.JSONB(none_as_null=True), sa.JSON())
