import logging

import sqlalchemy as sa
from alembic import op

revision = "0374ac83604b"
down_revision = "6d8eb8fba21b"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

WIREGUARD_MAX_KEEPALIVE = 65535
MYSQL_FAMILY = ("mysql", "mariadb")

CANDIDATES = {
    "sqlite": f"""
        SELECT id FROM (
            SELECT
                id,
                CASE WHEN json_valid(wireguard_overrides)
                    THEN json_type(wireguard_overrides, '$.keepalive_seconds') END AS keepalive_type,
                CASE WHEN json_valid(wireguard_overrides)
                    THEN json_extract(wireguard_overrides, '$.keepalive_seconds') END AS keepalive
            FROM hosts
            WHERE wireguard_overrides IS NOT NULL
        )
        WHERE keepalive_type IN ('integer', 'real') AND keepalive > {WIREGUARD_MAX_KEEPALIVE}
    """,
    "mysql": f"""
        SELECT id FROM hosts
        WHERE wireguard_overrides IS NOT NULL
          AND JSON_TYPE(JSON_EXTRACT(wireguard_overrides, '$.keepalive_seconds'))
              IN ('INTEGER', 'UNSIGNED INTEGER', 'DOUBLE', 'DECIMAL')
          AND JSON_EXTRACT(wireguard_overrides, '$.keepalive_seconds') > {WIREGUARD_MAX_KEEPALIVE}
    """,
    "postgresql": f"""
        SELECT id FROM hosts
        WHERE wireguard_overrides IS NOT NULL
          AND jsonb_typeof(wireguard_overrides::jsonb -> 'keepalive_seconds') = 'number'
          AND CASE WHEN jsonb_typeof(wireguard_overrides::jsonb -> 'keepalive_seconds') = 'number'
                THEN (wireguard_overrides::jsonb ->> 'keepalive_seconds')::numeric END > {WIREGUARD_MAX_KEEPALIVE}
    """,
}

hosts = sa.table("hosts", sa.column("id", sa.BigInteger()), sa.column("wireguard_overrides", sa.JSON()))


def _too_long(value) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and value > WIREGUARD_MAX_KEEPALIVE


def upgrade() -> None:
    bind = op.get_bind()
    dialect = "mysql" if bind.dialect.name in MYSQL_FAMILY else bind.dialect.name
    query = CANDIDATES.get(dialect)
    if query is None:
        return
    clamped = 0
    for host_id in [row[0] for row in bind.execute(sa.text(query))]:
        overrides = bind.execute(sa.select(hosts.c.wireguard_overrides).where(hosts.c.id == host_id)).scalar()
        if not isinstance(overrides, dict) or not _too_long(overrides.get("keepalive_seconds")):
            continue
        overrides = {**overrides, "keepalive_seconds": WIREGUARD_MAX_KEEPALIVE}
        bind.execute(sa.update(hosts).where(hosts.c.id == host_id).values(wireguard_overrides=overrides))
        clamped += 1
    if clamped:
        logger.warning("Clamped %d WireGuard keepalive override(s) to %d seconds", clamped, WIREGUARD_MAX_KEEPALIVE)


def downgrade() -> None:
    pass
