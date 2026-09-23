import logging

import sqlalchemy as sa
from alembic import op

revision = "6698b9a695e3"
down_revision = "b176d23e434c"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

VLESS_USER_FLOWS = ("", "xtls-rprx-vision", "xtls-rprx-vision-udp443")
MAX_HWID_LIMIT = 2**31 - 1
MAX_AUTO_DELETE_IN_DAYS = 36500

FLOW_CANDIDATES = {
    "sqlite": """
        SELECT id FROM (
            SELECT
                id,
                CASE WHEN json_valid(proxy_settings) THEN json_type(proxy_settings, '$.vless.flow') END AS flow_type,
                CASE WHEN json_valid(proxy_settings) THEN json_extract(proxy_settings, '$.vless.flow') END AS flow
            FROM users
        )
        WHERE flow_type IS NOT NULL
          AND flow_type != 'null'
          AND NOT (flow_type = 'text' AND flow IN ('', 'xtls-rprx-vision', 'xtls-rprx-vision-udp443'))
    """,
    "mysql": """
        SELECT id FROM users
        WHERE JSON_TYPE(JSON_EXTRACT(proxy_settings, '$.vless.flow')) IS NOT NULL
          AND JSON_TYPE(JSON_EXTRACT(proxy_settings, '$.vless.flow')) != 'NULL'
          AND NOT (
            JSON_TYPE(JSON_EXTRACT(proxy_settings, '$.vless.flow')) = 'STRING'
            AND JSON_UNQUOTE(JSON_EXTRACT(proxy_settings, '$.vless.flow')) IN ('', 'xtls-rprx-vision', 'xtls-rprx-vision-udp443')
          )
    """,
    "postgresql": """
        SELECT id FROM users
        WHERE jsonb_typeof(proxy_settings::jsonb -> 'vless' -> 'flow') IS NOT NULL
          AND jsonb_typeof(proxy_settings::jsonb -> 'vless' -> 'flow') != 'null'
          AND NOT (
            jsonb_typeof(proxy_settings::jsonb -> 'vless' -> 'flow') = 'string'
            AND proxy_settings::jsonb -> 'vless' ->> 'flow' IN ('', 'xtls-rprx-vision', 'xtls-rprx-vision-udp443')
          )
    """,
}

users = sa.table("users", sa.column("id", sa.BigInteger()), sa.column("proxy_settings", sa.JSON()))


def _clear_unusable_flows(bind) -> None:
    query = FLOW_CANDIDATES.get(bind.dialect.name)
    if query is None:
        return
    candidate_ids = [row[0] for row in bind.execute(sa.text(query))]
    cleared = 0
    for user_id in candidate_ids:
        settings = bind.execute(sa.select(users.c.proxy_settings).where(users.c.id == user_id)).scalar()
        vless = settings.get("vless") if isinstance(settings, dict) else None
        if not isinstance(vless, dict) or vless.get("flow") in (None, *VLESS_USER_FLOWS):
            continue
        settings = {**settings, "vless": {**vless, "flow": None}}
        bind.execute(sa.update(users).where(users.c.id == user_id).values(proxy_settings=settings))
        cleared += 1
    if cleared:
        logger.warning("Cleared %d VLESS flow(s) that no core accepts", cleared)


def _apply_if_any(bind, table: str, predicate: str, assignment: str, label: str) -> None:
    count = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table} WHERE {predicate}")).scalar()
    if count:
        bind.execute(sa.text(f"UPDATE {table} SET {assignment} WHERE {predicate}"))
        logger.warning("Fixed %d %s row(s): %s", count, table, label)


def upgrade() -> None:
    bind = op.get_bind()
    _clear_unusable_flows(bind)
    for table in ("users", "user_templates"):
        _apply_if_any(bind, table, "hwid_limit < 0", "hwid_limit = NULL", "negative hwid_limit")
        _apply_if_any(
            bind,
            table,
            f"hwid_limit > {MAX_HWID_LIMIT}",
            f"hwid_limit = {MAX_HWID_LIMIT}",
            "hwid_limit above the node range",
        )
    _apply_if_any(
        bind,
        "users",
        f"auto_delete_in_days > {MAX_AUTO_DELETE_IN_DAYS}",
        f"auto_delete_in_days = {MAX_AUTO_DELETE_IN_DAYS}",
        "auto_delete_in_days beyond 100 years",
    )


def downgrade() -> None:
    pass
