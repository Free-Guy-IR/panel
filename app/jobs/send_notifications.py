import asyncio
from datetime import datetime as dt, timezone as tz, timedelta as td
from typing import Any

import httpx
from sqlalchemy import delete
from fastapi.encoders import jsonable_encoder

from app import on_shutdown, async_scheduler as scheduler
from app.db import GetDB
from app.db.models import NotificationReminder
from app.notification.webhook import queue
from app.utils.logger import get_logger
from config import (
    JOB_SEND_NOTIFICATIONS_INTERVAL,
    NUMBER_OF_RECURRENT_NOTIFICATIONS,
    RECURRENT_NOTIFICATIONS_TIMEOUT,
    WEBHOOK_ADDRESS,
    WEBHOOK_SECRET,
    WEBHOOK_PROXY_URL,
)

logger = get_logger("send-notfication")

headers = {"x-webhook-secret": WEBHOOK_SECRET} if WEBHOOK_SECRET else None

client = httpx.AsyncClient(
    http2=True,
    timeout=httpx.Timeout(10),
    proxy=WEBHOOK_PROXY_URL,
)


async def send(data: dict[Any, Any]) -> bool:
    """Send the notification to the webhook address provided by WEBHOOK_ADDRESS

    Args:
        data (dict[Any, Any]): list of json encoded notifications

    Returns:
        bool: returns True if an ok response received
    """

    result_list = []
    for webhook in WEBHOOK_ADDRESS:
        result = await send_req(w_address=webhook, data=data)
        result_list.append(result)
    if True in result_list:
        return True
    else:
        return False


async def send_req(w_address: str, data):
    try:
        logger.debug(f"Sending {len(data)} webhook updates to {w_address}")
        r = await client.post(w_address, json=data, headers=headers)
        if r.status_code in [200, 201, 202, 204]:
            return True
        logger.error(r)
    except Exception as err:
        logger.error(err)
    return False


async def send_notifications():
    logger.debug("Processing notifications batch")

    processed = 0
    failed_to_requeue = []
    current_time = dt.now(tz.utc).timestamp()

    try:
        while True:
            try:
                # Get without waiting to process only existing items
                notification = queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            try:
                if notification.tries >= NUMBER_OF_RECURRENT_NOTIFICATIONS:
                    continue

                if notification.send_at > current_time:
                    failed_to_requeue.append(notification)
                    continue

                try:
                    success = await send(jsonable_encoder(notification))
                except Exception:
                    success = False

                if not success:
                    notification.tries += 1
                    if notification.tries < NUMBER_OF_RECURRENT_NOTIFICATIONS:
                        notification.send_at = current_time + RECURRENT_NOTIFICATIONS_TIMEOUT
                        failed_to_requeue.append(notification)

                processed += 1

            except Exception:
                failed_to_requeue.append(notification)

    finally:
        # Requeue failed items at the end
        for notif in failed_to_requeue:
            await queue.put(notif)

        if processed or failed_to_requeue:
            logger.debug(f"Processed {processed} notifications, requeued {len(failed_to_requeue)}")


async def delete_expired_reminders() -> None:
    async with GetDB() as db:
        # Get current UTC time and convert to naive datetime
        now_utc = dt.now(tz=tz.utc)
        now_naive = now_utc.replace(tzinfo=None)

        result = await db.execute(delete(NotificationReminder).where(NotificationReminder.expires_at < now_naive))
        logger.info(f"Cleaned up {result.rowcount} expired reminders")


async def send_pending_notifications_before_shutdown():
    logger.info("Webhook final flush before shutdown")
    await send_notifications()


if WEBHOOK_ADDRESS:
    logger.info("Webhook system initialized")
    scheduler.add_job(
        send_notifications, "interval", seconds=JOB_SEND_NOTIFICATIONS_INTERVAL, max_instances=1, coalesce=True
    )
    scheduler.add_job(delete_expired_reminders, "interval", hours=6, start_date=dt.now(tz.utc) + td(minutes=5))
    on_shutdown(send_pending_notifications_before_shutdown)
