import logging

import sqlalchemy as sa
from alembic import op

revision = "e9a7edd9de06"
down_revision = "f7c63e209a37"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

DELETE_ORPHANS = (
    ("connection_restrictions", "user_id", "users", "id"),
    ("user_connection_limits", "user_id", "users", "id"),
    ("user_connection_states", "user_id", "users", "id"),
    ("user_subscription_accesses", "user_id", "users", "id"),
    ("user_subscription_updates", "user_id", "users", "id"),
    ("user_usage_logs", "user_id", "users", "id"),
    ("user_hwids", "user_id", "users", "id"),
    ("notification_reminders", "user_id", "users", "id"),
    ("node_user_usages", "user_id", "users", "id"),
    ("next_plans", "user_id", "users", "id"),
    ("users_groups_association", "user_id", "users", "id"),
    ("users_groups_association", "groups_id", "groups", "id"),
    ("inbounds_groups_association", "group_id", "groups", "id"),
    ("inbounds_groups_association", "inbound_id", "inbounds", "id"),
    ("template_group_association", "group_id", "groups", "id"),
    ("template_group_association", "user_template_id", "user_templates", "id"),
    ("admin_notification_reminders", "admin_id", "admins", "id"),
    ("api_keys", "admin_id", "admins", "id"),
    ("admin_usage_logs", "admin_id", "admins", "id"),
    ("content_filter_assignments", "node_id", "nodes", "id"),
    ("content_filter_assignments", "profile_id", "content_filter_profiles", "id"),
    ("content_filter_sniffing_overrides", "core_id", "core_configs", "id"),
    ("node_additional_cores", "node_id", "nodes", "id"),
    ("node_additional_cores", "core_config_id", "core_configs", "id"),
    ("node_stats", "node_id", "nodes", "id"),
)

NULL_ORPHANS = (
    ("node_usages", "node_id", "nodes", "id"),
    ("node_user_usages", "node_id", "nodes", "id"),
    ("node_usage_reset_logs", "node_id", "nodes", "id"),
    ("node_inbound_usages", "node_id", "nodes", "id"),
    ("nodes", "core_config_id", "core_configs", "id"),
    ("next_plans", "user_template_id", "user_templates", "id"),
    ("hosts", "inbound_tag", "inbounds", "tag"),
    ("users", "admin_id", "admins", "id"),
)


def _dangling(table: str, column: str, parent: str, key: str) -> str:
    return (
        f"{table}.{column} IS NOT NULL AND NOT EXISTS "
        f"(SELECT 1 FROM {parent} WHERE {parent}.{key} = {table}.{column})"
    )


def _count(bind, table: str, predicate: str) -> int:
    return bind.execute(sa.text(f"SELECT COUNT(*) FROM {table} WHERE {predicate}")).scalar()


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        return

    roleless = _count(bind, "admins", _dangling("admins", "role_id", "admin_roles", "id"))
    if roleless:
        raise RuntimeError(
            f"{roleless} admin(s) point at an admin role that no longer exists; "
            "assign them an existing role before upgrading, this migration will not delete admins"
        )

    for table, column, parent, key in DELETE_ORPHANS:
        predicate = _dangling(table, column, parent, key)
        orphans = _count(bind, table, predicate)
        if orphans:
            bind.execute(sa.text(f"DELETE FROM {table} WHERE {predicate}"))
            logger.warning("Deleted %d %s row(s) whose %s no longer exists", orphans, table, column)

    for table, column, parent, key in NULL_ORPHANS:
        predicate = _dangling(table, column, parent, key)
        orphans = _count(bind, table, predicate)
        if orphans:
            bind.execute(sa.text(f"UPDATE {table} SET {column} = NULL WHERE {predicate}"))
            logger.warning("Cleared %d dangling %s.%s reference(s)", orphans, table, column)


def downgrade() -> None:
    pass
