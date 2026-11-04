from aiogram.client.session.aiohttp import AiohttpSession

from app.utils.http_client import INVALID_PROXY_MESSAGE, InvalidProxyURL, checked_proxy_url
from app.utils.ssl_context import create_outbound_ssl_context

TELEGRAM_PROXY_SCHEMES = ("http://", "socks4://", "socks5://")


def telegram_proxy_url(proxy: str | None) -> str | None:
    checked = checked_proxy_url(proxy)
    if checked is not None and not checked.startswith(TELEGRAM_PROXY_SCHEMES):
        raise InvalidProxyURL(INVALID_PROXY_MESSAGE)
    return checked


class NativeTLSAiohttpSession(AiohttpSession):
    """Use native OpenSSL with system and certifi roots for Telegram HTTPS."""

    def __init__(self, proxy=None, limit: int = 100, **kwargs):
        """Initialize aiogram and apply the scoped outbound SSL context."""
        super().__init__(proxy=telegram_proxy_url(proxy), limit=limit, **kwargs)
        # aiogram has no public SSLContext argument. It builds its connector
        # from this mapping for both direct and proxied sessions.
        self._connector_init["ssl"] = create_outbound_ssl_context()
