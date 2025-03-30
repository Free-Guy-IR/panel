import asyncio
from datetime import datetime as dt, timezone as tz, timedelta as td
from typing import Any

import httpx
from sqlalchemy import delete
from fastapi.encoders import jsonable_encoder

from app import logger, on_shutdown, async_scheduler as scheduler
from app.db import GetDB
from app.db.models import NotificationReminder
from app.notification.webhook import queue
from config import (
    JOB_SEND_NOTIFICATIONS_INTERVAL,
    NUMBER_OF_RECURRENT_NOTIFICATIONS,
    RECURRENT_NOTIFICATIONS_TIMEOUT,
    WEBHOOK_ADDRESS,
    WEBHOOK_SECRET,
    WEBHOOK_PROXY_URL,
)

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
    async def _put(notif):
        notif.tries += 1
        if notif.tries <= NUMBER_OF_RECURRENT_NOTIFICATIONS:
            notif.send_at = (dt.now(tz.utc) + td(seconds=RECURRENT_NOTIFICATIONS_TIMEOUT)).timestamp()
            await queue.put(notif)

    while True:
        try:
            notification = queue.get_nowait()
        except asyncio.QueueEmpty:
            return

        try:
            if notification.tries > NUMBER_OF_RECURRENT_NOTIFICATIONS:
                continue

            if notification.send_at > dt.now(tz.utc).timestamp():
                await queue.put(notification)
                continue

            if not await send(jsonable_encoder(notification)):
                await _put(notification)

        except Exception as err:
            logger.error(f"Error processing notification: {err}")
            await _put(notification)


async def delete_expired_reminders() -> None:
    async with GetDB() as db:
        await db.execute(delete(NotificationReminder).where(NotificationReminder.expires_at < dt.now(tz=tz.utc)))


async def send_pending_notifications_before_shutdown():
    logger.info("Sending pending notifications before shutdown...")
    await send_notifications()


if WEBHOOK_ADDRESS:
    logger.info("Send webhook job started")
    scheduler.add_job(send_notifications, "interval", seconds=JOB_SEND_NOTIFICATIONS_INTERVAL, replace_existing=True)
    scheduler.add_job(delete_expired_reminders, "interval", hours=2, start_date=dt.now(tz.utc) + td(minutes=1))
    on_shutdown(send_pending_notifications_before_shutdown)
