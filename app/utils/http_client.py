from urllib.parse import urlsplit

import aiohttp
import yarl

from app.models.validators import ProxyValidator
from app.utils.ssl_context import create_outbound_ssl_context

INVALID_PROXY_MESSAGE = "stored proxy URL is invalid; update the proxy setting"


class InvalidProxyURL(ValueError):
    pass


def _proxy_url_parses(proxy: str) -> bool:
    try:
        ProxyValidator.validate_proxy_url(proxy)
        parts = urlsplit(proxy)
        url = yarl.URL(proxy)
        return bool(parts.hostname) and bool(url.host) and 1 <= (parts.port or 0) <= 65535 and parts.port == url.port
    except Exception:
        return False


def checked_proxy_url(proxy: str | None) -> str | None:
    if not proxy:
        return None
    if not _proxy_url_parses(proxy):
        raise InvalidProxyURL(INVALID_PROXY_MESSAGE)
    return proxy


def create_outbound_http_session(*, proxy: str | None = None, timeout_seconds: float = 10) -> aiohttp.ClientSession:
    """Create an HTTP client whose TLS changes cannot affect server sockets."""
    checked_proxy = checked_proxy_url(proxy)
    return aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(ssl=create_outbound_ssl_context()),
        timeout=aiohttp.ClientTimeout(total=timeout_seconds),
        proxy=checked_proxy,
    )
