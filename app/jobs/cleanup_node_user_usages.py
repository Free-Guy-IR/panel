from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select

from app import scheduler
from app.db import GetDB
from app.db.models import NodeUserUsage
from app.utils.logger import get_logger
from config import job_settings, runtime_settings

logger = get_logger("jobs")

# Rows removed per statement, and the ceiling for one run. Small enough that
# the usage job writing to this table every 30 seconds is not held up; the
# existing backlog drains over several runs rather than in one long lock.
DELETE_CHUNK = 5_000
MAX_PER_RUN = 200_000


async def cleanup_node_user_usages():
    """Drop per-user, per-node usage history past the retention window.

    Accounting is unaffected: users.used_traffic and admins.used_traffic are
    separate running counters, not sums over this table. Only the per-node
    breakdown for old periods goes away.
    """
    retention_days = job_settings.node_user_usages_retention_days
    if retention_days <= 0:
        return

    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    deleted = 0

    async with GetDB() as db:
        while deleted < MAX_PER_RUN:
            # Select the ids first: a bare DELETE ... LIMIT is not portable
            # across the three dialects this panel supports.
            ids = (
                (
                    await db.execute(
                        select(NodeUserUsage.id).where(NodeUserUsage.created_at < cutoff).limit(DELETE_CHUNK)
                    )
                )
                .scalars()
                .all()
            )
            if not ids:
                break

            await db.execute(delete(NodeUserUsage).where(NodeUserUsage.id.in_(ids)))
            await db.commit()
            deleted += len(ids)

            if len(ids) < DELETE_CHUNK:
                break

    if deleted:
        logger.info(
            f"Removed {deleted} node user usage rows older than {retention_days} days"
            + (" (more remain, continuing next run)" if deleted >= MAX_PER_RUN else "")
        )


if job_settings.node_user_usages_retention_days > 0 and runtime_settings.role.runs_scheduler:
    scheduler.add_job(
        cleanup_node_user_usages,
        "interval",
        seconds=job_settings.cleanup_node_user_usages_interval,
        max_instances=1,
        id="cleanup_node_user_usages",
        replace_existing=True,
    )
