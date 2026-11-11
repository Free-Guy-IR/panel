import hashlib
import re

from pydantic import Field

from app.notification.nats_queue import QUEUE_MAX_MESSAGES, NotificationDelivery
from app.notification.queue_manager import DiscordNotification, TelegramNotification
from app.utils.logger import get_logger
from config import EnvSettings

logger = get_logger("Notification")

RATE_LIMIT_MAX_HOURS = 24
RATE_LIMIT_MAX_AGE = RATE_LIMIT_MAX_HOURS * 3600.0
RATE_LIMIT_DROP_MESSAGE = "Notification dropped after %d hours of rate limiting (%s)"
RATE_LIMIT_SHARE_DROP_MESSAGE = (
    "Notification dropped: %s already holds %d rate-limited notifications, its share of the queue"
)
HELD_SLOT_GRACE = 60.0

Notification = TelegramNotification | DiscordNotification


class RateLimitShareSettings(EnvSettings):
    destination_share: float = Field(
        default=0.1, gt=0, le=1, validation_alias="NOTIFICATION_RATE_LIMITED_DESTINATION_SHARE"
    )


rate_limit_share_settings = RateLimitShareSettings()
_held: dict[str, tuple[object, dict[float, float]]] = {}


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


def destination_label(destination: str) -> str:
    kind, _, target = destination.partition(":")
    if kind == "telegram":
        return f"Telegram chat {target}"
    webhook = re.search(r"/api/webhooks/(\d+)", target)
    if webhook:
        return f"Discord webhook {webhook.group(1)}"
    return f"Discord webhook {hashlib.sha256(target.encode()).hexdigest()[:12]}"


def destination_share(queue: object) -> int:
    capacity = getattr(queue, "maxsize", None) or QUEUE_MAX_MESSAGES
    return max(1, int(capacity * rate_limit_share_settings.destination_share))


def _held_slots(destination: str, limit: object, now: float) -> dict[float, float]:
    owner, slots = _held.get(destination, (None, {}))
    if owner is not limit:
        slots = {}
        _held[destination] = (limit, slots)
    for slot in [slot for slot, until in slots.items() if until + HELD_SLOT_GRACE < now]:
        del slots[slot]
    return slots


async def refused_over_destination_share(
    delivery: NotificationDelivery, notification: Notification, destination: str, limit, now: float
) -> bool:
    if notification.slot is not None and notification.slot >= limit.first_slot:
        return False
    held = _held_slots(destination, limit, now)
    if len(held) < destination_share(getattr(delivery, "queue", None)):
        return False
    logger.warning(RATE_LIMIT_SHARE_DROP_MESSAGE, destination_label(destination), len(held))
    await delivery.ack()
    return True


async def kept_by_rate_limit(
    delivery: NotificationDelivery,
    notification: Notification,
    destination: str,
    limit,
    slot: float,
    until: float,
    now: float,
) -> Notification | None:
    if await dropped_after_rate_limit_lifetime(delivery, notification, now):
        released_by_rate_limit(destination, slot)
        return None
    _held_slots(destination, limit, now)[slot] = until
    return held_by_rate_limit(notification, now)


def released_by_rate_limit(destination: str, slot: float | None) -> None:
    entry = _held.get(destination)
    if entry is not None and slot is not None:
        entry[1].pop(slot, None)
