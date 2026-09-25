from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.db.models import UserStatus
from app.models.user import UserNotificationResponse
from app.notification import webhook as webhook_notification

SECRET_UUID = "00000000-0000-4000-8000-000000000001"
SUBSCRIPTION_URL = "https://example.test/sub/bearer-token"


@pytest.fixture
def enqueued(monkeypatch):
    enqueue = AsyncMock()
    monkeypatch.setattr(webhook_notification, "webhook_settings", AsyncMock(return_value=SimpleNamespace(enable=True)))
    monkeypatch.setattr(webhook_notification, "enqueue_webhook", enqueue)
    return enqueue


def _user():
    return UserNotificationResponse(
        id=1,
        username="subscriber",
        status=UserStatus.active,
        used_traffic=0,
        created_at=datetime.now(UTC),
        subscription_url=SUBSCRIPTION_URL,
        proxy_settings={"vless": {"id": SECRET_UUID}},
    )


async def _send_both(user):
    await webhook_notification.notify(
        webhook_notification.ReachedUsagePercent(username=user.username, user=user, used_percent=80)
    )
    await webhook_notification.bulk_notify([webhook_notification.UserExpired(username=user.username, user=user)])


@pytest.mark.asyncio
async def test_webhook_payloads_carry_no_proxy_credentials(enqueued):
    await _send_both(_user())
    assert enqueued.await_count == 2
    for call in enqueued.await_args_list:
        payload = call.args[0]
        assert "proxy_settings" not in payload["user"]
        assert SECRET_UUID not in str(payload)
        assert payload["user"]["username"] == "subscriber"


@pytest.mark.asyncio
async def test_webhook_payloads_keep_the_subscription_url_until_the_owner_decides(enqueued):
    await _send_both(_user())
    for call in enqueued.await_args_list:
        assert call.args[0]["user"]["subscription_url"] == SUBSCRIPTION_URL
