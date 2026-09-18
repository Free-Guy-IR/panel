from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select

from app import scheduler
from app.db import GetDB
from app.db.models import NodeStat
from app.utils.logger import get_logger
from config import job_settings, runtime_settings

logger = get_logger("jobs")

DELETE_CHUNK = 5_000
MAX_PER_RUN = 200_000


async def cleanup_node_stats():
    retention_days = job_settings.node_stats_retention_days
    if retention_days <= 0:
        return

    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    deleted = 0

    async with GetDB() as db:
        while deleted < MAX_PER_RUN:
            ids = (
                (await db.execute(select(NodeStat.id).where(NodeStat.created_at < cutoff).limit(DELETE_CHUNK)))
                .scalars()
                .all()
            )
            if not ids:
                break

            await db.execute(delete(NodeStat).where(NodeStat.id.in_(ids)))
            await db.commit()
            deleted += len(ids)

            if len(ids) < DELETE_CHUNK:
                break

    if deleted:
        logger.info(
            f"Removed {deleted} node resource samples older than {retention_days} days"
            + (" (more remain, continuing next run)" if deleted >= MAX_PER_RUN else "")
        )


if job_settings.node_stats_retention_days > 0 and runtime_settings.role.runs_scheduler:
    scheduler.add_job(
        cleanup_node_stats,
        "interval",
        seconds=job_settings.cleanup_node_stats_interval,
        max_instances=1,
        id="cleanup_node_stats",
        replace_existing=True,
    )
