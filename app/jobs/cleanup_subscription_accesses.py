from sqlalchemy import delete, func, select

from app import scheduler
from app.db import GetDB
from app.db.models import UserSubscriptionAccess
from app.subscription.access_buffer import flush_subscription_accesses
from app.utils.logger import get_logger
from config import job_settings, runtime_settings, subscription_env_settings

logger = get_logger("jobs")


async def cleanup_user_subscription_accesses():
    limit = subscription_env_settings.access_limit
    if limit <= 0:
        return

    await flush_subscription_accesses()

    async with GetDB() as db:
        crowded = await db.execute(
            select(UserSubscriptionAccess.user_id, UserSubscriptionAccess.access_kind)
            .group_by(UserSubscriptionAccess.user_id, UserSubscriptionAccess.access_kind)
            .having(func.count(UserSubscriptionAccess.id) > limit)
        )
        groups = [(row.user_id, row.access_kind) for row in crowded]

        if not groups:
            logger.info("No users with excess subscription accesses")
            return

        dialect = db.bind.dialect.name

        if dialect == "mysql":
            total_deleted = 0
            for user_id, access_kind in groups:
                keep_ids_result = await db.execute(
                    select(UserSubscriptionAccess.id)
                    .where(
                        UserSubscriptionAccess.user_id == user_id,
                        UserSubscriptionAccess.access_kind == access_kind,
                    )
                    .order_by(UserSubscriptionAccess.created_at.desc(), UserSubscriptionAccess.id.desc())
                    .limit(limit)
                )
                keep_ids = [row.id for row in keep_ids_result]

                if keep_ids:
                    result = await db.execute(
                        delete(UserSubscriptionAccess).where(
                            UserSubscriptionAccess.user_id == user_id,
                            UserSubscriptionAccess.access_kind == access_kind,
                            UserSubscriptionAccess.id.not_in(keep_ids),
                        )
                    )
                    total_deleted += result.rowcount
        else:
            access = UserSubscriptionAccess.__table__.alias("access")

            keep_subquery = (
                select(access.c.id)
                .where(
                    access.c.user_id == UserSubscriptionAccess.user_id,
                    access.c.access_kind == UserSubscriptionAccess.access_kind,
                )
                .order_by(access.c.created_at.desc(), access.c.id.desc())
                .limit(limit)
            )

            result = await db.execute(
                delete(UserSubscriptionAccess).where(
                    UserSubscriptionAccess.user_id.in_({user_id for user_id, _ in groups}),
                    UserSubscriptionAccess.id.not_in(keep_subquery),
                )
            )
            total_deleted = result.rowcount

        await db.commit()
        logger.info(f"Cleaned up {total_deleted} old subscription accesses")


if subscription_env_settings.access_limit > 0 and runtime_settings.role.runs_scheduler:
    scheduler.add_job(
        cleanup_user_subscription_accesses,
        "interval",
        seconds=job_settings.cleanup_subscription_updates_interval,
        max_instances=1,
        id="cleanup_user_subscription_accesses",
        replace_existing=True,
    )
