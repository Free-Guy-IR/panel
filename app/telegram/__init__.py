from telebot import TeleBot, apihelper

from config import TELEGRAM_API_TOKEN, TELEGRAM_PROXY_URL

bot = None
if TELEGRAM_API_TOKEN:
    apihelper.proxy = {"http": TELEGRAM_PROXY_URL, "https": TELEGRAM_PROXY_URL}
    bot = TeleBot(TELEGRAM_API_TOKEN)

handler_names = ["admin", "report", "user"]


from .handlers.report import (  # noqa
    report,
    report_new_user,
    report_user_modification,
    report_user_deletion,
    report_status_change,
    report_user_usage_reset,
    report_user_data_reset_by_next,
    report_user_subscription_revoked,
    report_login,
)

__all__ = [
    "bot",
    "report",
    "report_new_user",
    "report_user_modification",
    "report_user_deletion",
    "report_status_change",
    "report_user_usage_reset",
    "report_user_data_reset_by_next",
    "report_user_subscription_revoked",
    "report_login",
]
