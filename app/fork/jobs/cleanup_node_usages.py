from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select

from app import scheduler
from app.db import GetDB
from app.db.models import NodeInboundUsage, NodeUsage
from app.utils.logger import get_logger
from config import job_settings, runtime_settings

logger = get_logger("jobs")

DELETE_CHUNK = 5_000
MAX_PER_RUN = 200_000


async def _prune_in_chunks(model, cutoff: datetime) -> int:
    deleted = 0

    async with GetDB() as db:
        while deleted < MAX_PER_RUN:
            ids = (
                (await db.execute(select(model.id).where(model.created_at < cutoff).limit(DELETE_CHUNK)))
                .scalars()
                .all()
            )
            if not ids:
                break

            await db.execute(delete(model).where(model.id.in_(ids)))
            await db.commit()
            deleted += len(ids)

            if len(ids) < DELETE_CHUNK:
                break

    return deleted


async def cleanup_node_usages():
    retention_days = job_settings.node_usages_retention_days
    if retention_days <= 0:
        return

    cutoff = datetime.now(UTC) - timedelta(days=retention_days)

    for model, description in ((NodeUsage, "node usage rows"), (NodeInboundUsage, "node inbound usage rows")):
        deleted = await _prune_in_chunks(model, cutoff)
        if deleted:
            logger.info(
                f"Removed {deleted} {description} older than {retention_days} days"
                + (" (more remain, continuing next run)" if deleted >= MAX_PER_RUN else "")
            )


if job_settings.node_usages_retention_days > 0 and runtime_settings.role.runs_scheduler:
    scheduler.add_job(
        cleanup_node_usages,
        "interval",
        seconds=job_settings.cleanup_node_usages_interval,
        max_instances=1,
        id="cleanup_node_usages",
        replace_existing=True,
    )
