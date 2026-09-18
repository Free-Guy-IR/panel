from aiocache import cached

from app.db import GetDB
from app.db.crud.settings import get_settings
from app.db.models import Settings
from app.models import settings


class SettingsRowMissing(RuntimeError):
    pass


async def stored_settings() -> Settings:
    async with GetDB() as db:
        db_settings = await get_settings(db)

    if db_settings is None:
        raise SettingsRowMissing(
            "The settings row is missing from this database. It is created by migration 9af04c077ede, "
            "so a database whose schema was loaded without running the migrations will not have it. "
            "Run `alembic upgrade head` against this database, then start the panel again."
        )

    return db_settings


@cached()
async def telegram_settings() -> settings.Telegram:
    return settings.Telegram.model_validate((await stored_settings()).telegram)


@cached()
async def webhook_settings() -> settings.Webhook:
    return settings.Webhook.model_validate((await stored_settings()).webhook)


@cached()
async def notification_settings() -> settings.NotificationSettings:
    return settings.NotificationSettings.model_validate((await stored_settings()).notification_settings)


@cached()
async def notification_enable() -> settings.NotificationEnable:
    return settings.NotificationEnable.model_validate((await stored_settings()).notification_enable)


@cached()
async def subscription_settings() -> settings.Subscription:
    return settings.Subscription.model_validate((await stored_settings()).subscription)


@cached()
async def hwid_settings() -> settings.HWIDSettings:
    return settings.HWIDSettings.model_validate((await stored_settings()).hwid)


@cached()
async def general_settings() -> settings.General:
    return settings.General.model_validate((await stored_settings()).general)


async def refresh_caches() -> None:
    await telegram_settings.cache.clear()
    await webhook_settings.cache.clear()
    await notification_settings.cache.clear()
    await notification_enable.cache.clear()
    await subscription_settings.cache.clear()
    await hwid_settings.cache.clear()
    await general_settings.cache.clear()


async def handle_settings_message(_: dict):
    """Handle settings update message from NATS router."""
    await refresh_caches()
    try:
        from app.telegram import telegram_bot_manager
    except Exception:
        return
    await telegram_bot_manager.sync_from_settings()
