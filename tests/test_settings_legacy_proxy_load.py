import logging
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app import settings as app_settings
from app.models.settings import SettingsSchema, SettingsSchemaInput
from app.utils.http_client import INVALID_PROXY_MESSAGE

SECRET = "pw" + "Leak" + "Marker" + str(7)
HOST = "proxy.example"

LEGACY_PROXIES = [
    f"http://ux:{SECRET}#x@{HOST}:8080",
    f"socks5://ux:{SECRET}＃@{HOST}:1080",
    f"http://ux:{SECRET}@{HOST}:99999",
    f"http://ux:{SECRET}@{HOST}:8080\n",
    f"socks5://ux:{SECRET}Ā@{HOST}:1080",
]


def _row(proxy: str) -> SimpleNamespace:
    return SimpleNamespace(
        telegram={
            "enable": False,
            "token": "1234567890:" + "A" * 35,
            "webhook_url": f"https://{HOST}/tg",
            "webhook_secret": "s3cret",
            "proxy_url": proxy,
            "method": "long-polling",
            "mini_app_login": False,
            "mini_app_web_url": "",
            "for_admins_only": True,
        },
        webhook={
            "enable": True,
            "webhooks": [{"url": f"https://{HOST}/hook", "secret": "hooksecret"}],
            "days_left": [1, 3],
            "usage_percent": [80],
            "timeout": 7,
            "recurrent": 4,
            "proxy_url": proxy,
        },
        notification_settings={
            "notify_telegram": True,
            "notify_discord": False,
            "telegram_api_token": "1234567890:" + "A" * 35,
            "telegram_chat_id": 4242,
            "proxy_url": proxy,
            "max_retries": 5,
        },
        notification_enable=None,
        subscription=None,
        hwid=None,
        general=None,
        connection_limit=None,
    )


@pytest.fixture
async def stored_row(monkeypatch):
    holder = {}

    async def fake_stored_settings():
        return holder["row"]

    monkeypatch.setattr(app_settings, "stored_settings", fake_stored_settings)
    await app_settings.refresh_caches()
    yield holder
    await app_settings.refresh_caches()


def test_settings_schema_loads_a_legacy_stored_proxy_and_keeps_the_rest():
    for proxy in LEGACY_PROXIES:
        loaded = SettingsSchema.model_validate(_row(proxy))
        assert loaded.telegram.proxy_url == proxy
        assert loaded.telegram.token == "1234567890:" + "A" * 35
        assert loaded.webhook.proxy_url == proxy
        assert loaded.webhook.timeout == 7
        assert loaded.webhook.webhooks[0].url == f"https://{HOST}/hook"
        assert loaded.notification_settings.proxy_url == proxy
        assert loaded.notification_settings.telegram_chat_id == 4242
        assert loaded.notification_settings.max_retries == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("proxy", LEGACY_PROXIES)
async def test_cached_getters_load_a_legacy_stored_proxy(stored_row, proxy):
    stored_row["row"] = _row(proxy)
    telegram = await app_settings.telegram_settings()
    webhook = await app_settings.webhook_settings()
    notification = await app_settings.notification_settings()
    assert telegram.proxy_url == proxy
    assert telegram.webhook_secret == "s3cret"
    assert webhook.proxy_url == proxy
    assert webhook.recurrent == 4
    assert notification.proxy_url == proxy
    assert notification.notify_telegram is True


@pytest.mark.asyncio
@pytest.mark.parametrize("nats_enabled", [False, True])
@pytest.mark.parametrize("proxy", LEGACY_PROXIES)
async def test_loaded_legacy_proxy_is_refused_by_every_sender(stored_row, proxy, nats_enabled, caplog, monkeypatch):
    import app.jobs.send_notifications as job
    import app.notification.client as notification_client
    import app.telegram as telegram_module
    from app.telegram import TelegramBotManager

    monkeypatch.setattr(telegram_module, "is_nats_enabled", lambda: nats_enabled)
    stored_row["row"] = _row(proxy)
    caplog.set_level(logging.DEBUG)

    monkeypatch.setattr(notification_client, "client", None)
    await notification_client.define_client()
    assert notification_client.client is None

    dequeued = []

    class _Queue:
        async def dequeue(self, timeout=1):
            dequeued.append(timeout)

    monkeypatch.setattr(job, "get_webhook_queue", lambda: _Queue())
    await job.send_notifications()
    assert dequeued == []

    manager = TelegramBotManager()
    await manager._start_locked(await app_settings.telegram_settings())
    assert manager._bot is None

    messages = [record.getMessage() for record in caplog.records]
    if nats_enabled:
        assert any(message.startswith("Long polling is not supported in multi-worker mode") for message in messages)
        assert messages.count(INVALID_PROXY_MESSAGE) >= 2
    else:
        assert messages.count(INVALID_PROXY_MESSAGE) >= 3
    assert all(SECRET not in message for message in messages)


@pytest.mark.parametrize("proxy", LEGACY_PROXIES)
@pytest.mark.parametrize("section", ["telegram", "webhook", "notification_settings"])
def test_settings_input_still_rejects_a_legacy_proxy(proxy, section):
    payload = {section: getattr(_row(proxy), section)}
    with pytest.raises(ValidationError) as raised:
        SettingsSchemaInput.model_validate(payload)
    assert any(error["loc"][:2] == (section, "proxy_url") for error in raised.value.errors())
