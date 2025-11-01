import asyncio

from app import on_shutdown, scheduler
from app.notification.client import process_telegram_queue, process_discord_queue
from app.utils.logger import get_logger
from config import JOB_SEND_NOTIFICATIONS_INTERVAL

logger = get_logger("process-notification-queues")


async def process_all_notification_queues():
    """
    Process both Telegram and Discord notification queues concurrently.
    Each queue sends messages one by one internally.
    """
    logger.debug("Processing notification queues")

    await asyncio.gather(process_telegram_queue(), process_discord_queue(), return_exceptions=True)


# Schedule the job to run at the same interval as webhook notifications
scheduler.add_job(
    process_all_notification_queues, "interval", seconds=JOB_SEND_NOTIFICATIONS_INTERVAL, max_instances=1, coalesce=True
)


async def send_pending_notifications_before_shutdown():
    logger.info("Webhook final flush before shutdown")
    await process_all_notification_queues()


on_shutdown(send_pending_notifications_before_shutdown)
