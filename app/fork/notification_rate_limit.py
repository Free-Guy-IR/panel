import hashlib
import json
import re
from typing import TYPE_CHECKING

from nats.js.errors import NotFoundError
from pydantic import Field

from app.utils.logger import get_logger
from config import EnvSettings

if TYPE_CHECKING:
    from app.notification.nats_queue import NotificationDelivery
    from app.notification.queue_manager import DiscordNotification, TelegramNotification

    Notification = TelegramNotification | DiscordNotification

logger = get_logger("Notification")

RATE_LIMIT_MAX_HOURS = 24
RATE_LIMIT_MAX_AGE = RATE_LIMIT_MAX_HOURS * 3600.0
RATE_LIMIT_DROP_MESSAGE = "Notification dropped after %d hours of rate limiting (%s)"
RATE_LIMIT_SHARE_DROP_MESSAGE = (
    "Notification dropped: %s already holds %d rate-limited notifications, its share of the queue"
)
HELD_SLOT_GRACE = 60.0


class RateLimitShareSettings(EnvSettings):
    destination_share: float = Field(
        default=0.1, gt=0, le=1, validation_alias="NOTIFICATION_RATE_LIMITED_DESTINATION_SHARE"
    )


rate_limit_share_settings = RateLimitShareSettings()
_held: dict[str, dict[float, float]] = {}


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
    from app.notification.nats_queue import QUEUE_MAX_MESSAGES

    capacity = getattr(queue, "maxsize", None) or QUEUE_MAX_MESSAGES
    return max(1, int(capacity * rate_limit_share_settings.destination_share))


def _held_slots(destination: str, now: float) -> dict[float, float]:
    slots = _held.setdefault(destination, {})
    for slot in [slot for slot, until in slots.items() if until + HELD_SLOT_GRACE < now]:
        del slots[slot]
    return slots


def _registered(notification: Notification, held: dict[float, float], limit) -> bool:
    if notification.slot is None:
        return False
    if notification.slot in held:
        return True
    return limit is not None and notification.slot >= limit.first_slot


async def _refuse(delivery: NotificationDelivery, destination: str, held: dict[float, float]) -> None:
    logger.warning(RATE_LIMIT_SHARE_DROP_MESSAGE, destination_label(destination), len(held))
    await delivery.ack()


async def refused_over_destination_share(
    delivery: NotificationDelivery, notification: Notification, destination: str, limit, now: float
) -> bool:
    held = _held_slots(destination, now)
    if _registered(notification, held, limit):
        return False
    if len(held) < destination_share(getattr(delivery, "queue", None)):
        return False
    await _refuse(delivery, destination, held)
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
    held = _held_slots(destination, now)
    if await dropped_after_rate_limit_lifetime(delivery, notification, now):
        released_by_rate_limit(destination, slot)
        released_by_rate_limit(destination, notification.slot)
        return None
    joining = not _registered(notification, held, limit) and slot not in held
    if joining and len(held) >= destination_share(getattr(delivery, "queue", None)):
        await _refuse(delivery, destination, held)
        return None
    if notification.slot is not None and notification.slot != slot:
        held.pop(notification.slot, None)
    held[slot] = until
    return held_by_rate_limit(notification, now)


def released_by_rate_limit(destination: str, slot: float | None) -> None:
    held = _held.get(destination)
    if held is not None and slot is not None:
        held.pop(slot, None)


def destination_of(item: dict) -> str | None:
    if item.get("type") == "discord" and item.get("webhook"):
        return f"discord:{item['webhook']}"
    if item.get("type") == "telegram" and item.get("chat_id"):
        return f"telegram:{item['chat_id']}"
    return None


async def _queued_items(queue: object) -> list:
    items = getattr(queue, "_items", None)
    if items is not None:
        return [entry[2] for entry in items]
    js = getattr(queue, "_js", None)
    if js is None:
        return []
    found = []
    sequence = 1
    while True:
        try:
            checkpoint = await js.get_msg(
                queue.RETRY_STREAM, seq=sequence, subject=f"{queue.RETRY_SUBJECT}.*", next=True
            )
        except NotFoundError:
            return found
        sequence = checkpoint.seq + 1
        try:
            state = json.loads(checkpoint.data)
        except ValueError:
            continue
        if not isinstance(state, dict) or state.get("completed"):
            continue
        try:
            await js.get_msg(queue.STREAM_NAME, seq=int(checkpoint.subject.rsplit(".", 1)[1]))
        except NotFoundError:
            continue
        found.append(state.get("data"))


async def seed_held_from_queue(queue: object, now: float) -> int:
    try:
        items = await _queued_items(queue)
    except Exception as error:
        logger.warning("Could not count the rate-limited notifications already queued: %s", type(error).__name__)
        return 0
    seeded = 0
    for item in items:
        if not isinstance(item, dict) or item.get("rate_limited_since") is None:
            continue
        destination = destination_of(item)
        slot = item.get("slot")
        if destination is None or not isinstance(slot, (int, float)):
            continue
        send_at = item.get("send_at")
        until = max(float(send_at) if isinstance(send_at, (int, float)) else now, now)
        _held_slots(destination, now)[float(slot)] = until
        seeded += 1
    if seeded:
        logger.info("Counted %d rate-limited notifications already in the queue", seeded)
    return seeded
