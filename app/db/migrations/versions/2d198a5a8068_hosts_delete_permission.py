import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "2d198a5a8068"
down_revision = "b708c8d4fc31"
branch_labels = None
depends_on = None

PERMISSION_TABLES = ("admin_roles", "api_keys")


def _permissions_table(name: str):
    permissions_type = sa.JSON().with_variant(postgresql.JSONB(none_as_null=True), "postgresql")
    return sa.table(name, sa.column("id", sa.BigInteger()), sa.column("permissions", permissions_type))


def _as_dict(value):
    if isinstance(value, (str, bytes)):
        value = json.loads(value)
    return value if isinstance(value, dict) else None


def _with_host_delete(permissions: dict) -> dict | None:
    hosts = permissions.get("hosts")
    if not isinstance(hosts, dict) or "delete" in hosts or not hosts.get("update"):
        return None
    return {**permissions, "hosts": {**hosts, "delete": hosts["update"]}}


def _without_host_delete(permissions: dict) -> dict | None:
    hosts = permissions.get("hosts")
    if not isinstance(hosts, dict) or "delete" not in hosts:
        return None
    return {**permissions, "hosts": {action: value for action, value in hosts.items() if action != "delete"}}


def _rewrite(change) -> None:
    bind = op.get_bind()
    for name in PERMISSION_TABLES:
        table = _permissions_table(name)
        rows = bind.execute(sa.select(table.c.id, table.c.permissions).order_by(table.c.id)).all()
        for row_id, stored in rows:
            permissions = _as_dict(stored)
            if permissions is None:
                continue
            changed = change(permissions)
            if changed is not None:
                bind.execute(sa.update(table).where(table.c.id == row_id).values(permissions=changed))


def upgrade() -> None:
    _rewrite(_with_host_delete)


def downgrade() -> None:
    _rewrite(_without_host_delete)
