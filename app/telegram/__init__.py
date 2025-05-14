from asyncio import Lock

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError

from app import on_shutdown, on_startup
from app.utils.logger import get_logger
from app.models.settings import Telegram
from app.settings import telegram_settings

from .handlers import include_routers
from .middlewares import setup_middlewares

logger = get_logger("telegram-bot")


bot = None
dp = None
_lock = Lock()


async def startup_telegram_bot():
    if bot:
        await shutdown_telegram_bot()

    global bot
    global dp
    
    async with _lock:
        settings: Telegram = await telegram_settings()
        if not settings.enable:
            return

        session = AiohttpSession(proxy=settings.proxy_url)
        bot = Bot(token=settings.token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        dp = Dispatcher()

        # register handlers
        include_routers(dp)
        # register middlewares
        setup_middlewares(dp)
        # register webhook
        webhook_address = f"{settings.webhook_url}/api/tghook"
        logger.info(webhook_address)
        try:
            await bot.set_webhook(
                webhook_address,
                secret_token=settings.webhook_secret,
                allowed_updates=["message", "callback_query", "inline_query"],
            )
            logger.info("telegram bot started successfully.")
        except TelegramNetworkError as err:
            logger.error(err.message)


async def shutdown_telegram_bot():
    global bot
    global dp

    async with _lock:
        if not bot:
            return
        try:
            await bot.delete_webhook(drop_pending_updates=True)
        except TelegramNetworkError as err:
            logger.error(err.message)
        await dp.storage.close()

        bot = None
        dp = None


on_startup(startup_telegram_bot)
on_shutdown(shutdown_telegram_bot)
