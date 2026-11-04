from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.db.models import UserStatus
from app.models.admin import AdminContactInfo, AdminDetails, AdminInDB
from app.models.user import UserNotificationResponse
from app.notification import webhook as webhook_notification

OWNER_DISCORD_WEBHOOK = "https://discord.example.com/api/webhooks/1/owner-admin-webhook-token"
ACTING_DISCORD_WEBHOOK = "https://discord.example.com/api/webhooks/2/acting-admin-webhook-token"
HASHED_PASSWORD = "$2b$12$acting-admin-hashed-password-material"
SECRET_UUID = "00000000-0000-4000-8000-000000000002"
OWNER_TELEGRAM_ID = 111111
ACTING_TELEGRAM_ID = 222222

FORBIDDEN_KEYS = {"discord_webhook", "hashed_password", "password", "proxy_settings"}
FORBIDDEN_VALUES = {OWNER_DISCORD_WEBHOOK, ACTING_DISCORD_WEBHOOK, HASHED_PASSWORD, SECRET_UUID}


@pytest.fixture
def enqueued(monkeypatch):
    enqueue = AsyncMock()
    monkeypatch.setattr(webhook_notification, "webhook_settings", AsyncMock(return_value=SimpleNamespace(enable=True)))
    monkeypatch.setattr(webhook_notification, "enqueue_webhook", enqueue)
    return enqueue


def _walk(node, path=""):
    if isinstance(node, dict):
        for key, value in node.items():
            yield f"{path}.{key}" if path else key, key, value
            yield from _walk(value, f"{path}.{key}" if path else key)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk(value, f"{path}[{index}]")


def _leaks(payload):
    found = []
    for path, key, value in _walk(payload):
        if key in FORBIDDEN_KEYS:
            found.append(path)
        if isinstance(value, str) and any(secret in value for secret in FORBIDDEN_VALUES):
            found.append(f"{path}=<secret>")
    return found


def _user():
    return UserNotificationResponse(
        id=1,
        username="subscriber",
        status=UserStatus.active,
        used_traffic=0,
        created_at=datetime.now(UTC),
        subscription_url="https://example.test/sub/token",
        proxy_settings={"vless": {"id": SECRET_UUID}},
        admin=AdminContactInfo(
            id=7, username="owner", telegram_id=OWNER_TELEGRAM_ID, discord_webhook=OWNER_DISCORD_WEBHOOK
        ),
    )


def _acting_admins():
    return [
        AdminDetails(id=8, username="actor", telegram_id=ACTING_TELEGRAM_ID, discord_webhook=ACTING_DISCORD_WEBHOOK),
        AdminInDB(
            id=8,
            username="actor",
            telegram_id=ACTING_TELEGRAM_ID,
            discord_webhook=ACTING_DISCORD_WEBHOOK,
            hashed_password=HASHED_PASSWORD,
        ),
    ]


def _every_notification(user, by):
    wh = webhook_notification
    name = user.username
    return [
        wh.ReachedUsagePercent(username=name, user=user, used_percent=80),
        wh.ReachedDaysLeft(username=name, user=user, days_left=3),
        wh.UserCreated(username=name, user=user, by=by),
        wh.UserUpdated(username=name, user=user, by=by),
        wh.UserDeleted(username=name, user=user, by=by),
        wh.UserLimited(username=name, user=user),
        wh.UserExpired(username=name, user=user),
        wh.UserEnabled(username=name, user=user, by=by),
        wh.UserEnabled(username=name, user=user),
        wh.UserDisabled(username=name, user=user, by=by, reason="manual"),
        wh.UserDisabled(username=name, user=user),
        wh.UserDataUsageReset(username=name, user=user, by=by),
        wh.UserDataResetByNext(username=name, user=user),
        wh.UserSubscriptionRevoked(username=name, user=user, by=by),
    ]


async def _deliver_everything():
    user = _user()
    for by in _acting_admins():
        messages = _every_notification(user, by)
        for message in messages:
            await webhook_notification.notify(message)
        await webhook_notification.bulk_notify(messages)
    for status in (UserStatus.limited, UserStatus.expired, UserStatus.disabled, UserStatus.active):
        await webhook_notification.status_change(user.model_copy(update={"status": status}))


@pytest.mark.asyncio
async def test_no_webhook_payload_carries_admin_or_user_secrets_at_any_depth(enqueued):
    await _deliver_everything()
    assert enqueued.await_count == 2 * 2 * 14 + 4
    leaks = [leak for call in enqueued.await_args_list for leak in _leaks(call.args[0])]
    assert leaks == [], f"{len(leaks)} leaked fields: {sorted(set(leaks))}"


@pytest.mark.asyncio
async def test_webhook_payloads_keep_the_public_admin_fields(enqueued):
    await _deliver_everything()
    payloads = [call.args[0] for call in enqueued.await_args_list]
    for payload in payloads:
        assert payload["user"]["username"] == "subscriber"
        assert payload["user"]["admin"]["username"] == "owner"
        assert payload["user"]["admin"]["telegram_id"] == OWNER_TELEGRAM_ID
    acted = [payload for payload in payloads if payload.get("by")]
    assert len(acted) == 2 * 2 * 7
    for payload in acted:
        assert payload["by"]["username"] == "actor"
        assert payload["by"]["telegram_id"] == ACTING_TELEGRAM_ID
