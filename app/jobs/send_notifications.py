import asyncio
from datetime import UTC, datetime as dt, timedelta as td

import aiohttp
from sqlalchemy import delete

from app import on_shutdown, scheduler
from app.db import GetDB
from app.db.models import NotificationReminder
from app.fork.jobs import webhook_request
from app.models.settings import Webhook
from app.notification.nats_queue import NotificationQueue
from app.notification.queue_manager import (
    WebhookNotification,
    enqueue_webhook,
    get_webhook_queue,
    shutdown_webhook_queue,
)
from app.settings import webhook_settings
from app.utils.http_client import create_outbound_http_session
from app.utils.logger import get_logger
from config import job_settings, runtime_settings

logger = get_logger("send-notification")


async def send_to_all_webhooks(client: aiohttp.ClientSession, notifications, webhooks):
    """
    Send the notifications to all webhooks concurrently.
    Returns True if at least one webhook succeeds.
    notifications: list of already JSON-serializable dicts (webhook payloads)
    """
    if not notifications:
        return True

    payload = notifications  # Already JSON-serializable, no need for jsonable_encoder

    async def send_one(webhook):
        body, webhook_headers = webhook_request(payload, webhook.secret)
        try:
            r = await client.post(webhook.url, data=body, headers=webhook_headers)
            if r.status in (200, 201, 202, 204):
                return True
            else:
                logger.error(f"Webhook {webhook.url} failed: {r.status} - {await r.text()}")
        except Exception as err:
            logger.error(f"Webhook {webhook.url} exception: {err}")
        return False

    results = await asyncio.gather(*(send_one(webhook) for webhook in webhooks))
    return any(results)


async def _settle(queue: NotificationQueue, unsettled: list[dict], items: list[dict]) -> None:
    settled_ids = {id(item) for item in items}
    unsettled[:] = [item for item in unsettled if id(item) not in settled_ids]
    for item in items:
        await queue.ack(item)


async def send_notifications():
    """Drain queued webhooks through a client-scoped trusted TLS context."""
    settings: Webhook = await webhook_settings()
    if not settings.enable:
        return

    logger.debug("Processing notifications batch")

    processed = 0
    failed_to_requeue: list[tuple[dict, WebhookNotification]] = []
    ready_notifications: list[tuple[dict, WebhookNotification]] = []
    unsettled: list[dict] = []
    current_time = dt.now(UTC).timestamp()
    webhook_queue = get_webhook_queue()
    cancelled = False

    try:
        async with create_outbound_http_session(proxy=settings.proxy_url if settings.proxy_url else None) as client:
            while True:
                try:
                    item = await webhook_queue.dequeue(timeout=1)
                except Exception:
                    # Handle any dequeue errors gracefully
                    break

                if not item:
                    break

                unsettled.append(item)
                try:
                    notification = WebhookNotification(**item)
                except Exception as err:
                    logger.error(f"Dropping an unreadable webhook notification: {err}")
                    await _settle(webhook_queue, unsettled, [item])
                    continue

                if notification.tries >= settings.recurrent:
                    logger.warning(f"Dropping a webhook notification after {notification.tries} failed tries")
                    await _settle(webhook_queue, unsettled, [item])
                    continue

                if notification.send_at > current_time:
                    failed_to_requeue.append((item, notification))
                    continue

                ready_notifications.append((item, notification))

            if ready_notifications:
                batch_size = 50
                for start in range(0, len(ready_notifications), batch_size):
                    batch = ready_notifications[start : start + batch_size]
                    logger.info(
                        f"Sending batch of {len(batch)} notifications to {len(settings.webhooks)} webhooks "
                        f"(chunk {start // batch_size + 1})"
                    )
                    # Extract payloads from WebhookNotification objects
                    payloads = [notif.payload for _, notif in batch]
                    success = await send_to_all_webhooks(client, payloads, settings.webhooks)

                    if success:
                        await _settle(webhook_queue, unsettled, [item for item, _ in batch])
                    else:
                        retry_at = dt.now(UTC).timestamp()
                        for item, notification in batch:
                            notification.tries += 1
                            if notification.tries < settings.recurrent:
                                notification.send_at = retry_at + settings.timeout
                                failed_to_requeue.append((item, notification))
                            else:
                                logger.warning(
                                    f"Dropping a webhook notification after {notification.tries} failed tries"
                                )
                                await _settle(webhook_queue, unsettled, [item])

                    processed += len(batch)

    except asyncio.CancelledError:
        cancelled = True
        await webhook_queue.release(*unsettled)
        unsettled.clear()
        raise
    finally:
        if not cancelled:
            # Requeue failed items at the end
            for item, notif in failed_to_requeue:
                try:
                    await enqueue_webhook(notif.payload, send_at=notif.send_at, tries=notif.tries)
                except Exception as err:
                    logger.error(f"Could not requeue a webhook notification; handing the original back: {err}")
                    continue
                await _settle(webhook_queue, unsettled, [item])
            if unsettled:
                await webhook_queue.release(*unsettled)

        if processed or failed_to_requeue:
            logger.info(f"Processed {processed} notifications, requeued {len(failed_to_requeue)}")


async def delete_expired_reminders() -> None:
    async with GetDB() as db:
        # Get current UTC time and convert to naive datetime
        now_utc = dt.now(tz=UTC)
        now_naive = now_utc.replace(tzinfo=None)

        result = await db.execute(delete(NotificationReminder).where(NotificationReminder.expires_at < now_naive))
        logger.info(f"Cleaned up {result.rowcount} expired reminders")


async def send_pending_notifications_before_shutdown():
    logger.info("Webhook final flush before shutdown")
    await send_notifications()


if runtime_settings.role.runs_scheduler:
    scheduler.add_job(
        send_notifications,
        "interval",
        seconds=job_settings.send_notifications_interval,
        max_instances=1,
        coalesce=True,
        id="send_notifications",
        replace_existing=True,
    )
    scheduler.add_job(
        delete_expired_reminders,
        "interval",
        hours=6,
        start_date=dt.now(UTC) + td(minutes=5),
        id="delete_expired_notification_reminders",
        replace_existing=True,
    )
    on_shutdown(send_pending_notifications_before_shutdown)
    on_shutdown(shutdown_webhook_queue)  # Must run after flush to keep queue alive
