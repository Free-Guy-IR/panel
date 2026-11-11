from app.notification.nats_queue import NotificationDelivery
from app.notification.queue_manager import DiscordNotification, TelegramNotification
from app.utils.logger import get_logger

logger = get_logger("Notification")

RATE_LIMIT_MAX_HOURS = 24
RATE_LIMIT_MAX_AGE = RATE_LIMIT_MAX_HOURS * 3600.0
RATE_LIMIT_DROP_MESSAGE = "Notification dropped after %d hours of rate limiting (%s)"

Notification = TelegramNotification | DiscordNotification


def held_by_rate_limit(notification: Notification, now: float) -> Notification:
    if notification.rate_limited_since is not None:
        return notification
    return notification.model_copy(update={"rate_limited_since": now})


async def dropped_after_rate_limit_lifetime(
    delivery: NotificationDelivery, notification: Notification, now: float
) -> bool:
    since = notification.rate_limited_since
    if since is None or now - since < RATE_LIMIT_MAX_AGE:
        return False
    logger.warning(RATE_LIMIT_DROP_MESSAGE, RATE_LIMIT_MAX_HOURS, notification.type)
    await delivery.ack()
    return True
