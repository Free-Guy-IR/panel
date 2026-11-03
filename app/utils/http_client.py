import aiohttp

from app.models.validators import ProxyValidator
from app.utils.ssl_context import create_outbound_ssl_context

INVALID_PROXY_MESSAGE = "stored proxy URL is invalid; update the proxy setting"


class InvalidProxyURL(ValueError):
    pass


def checked_proxy_url(proxy: str | None) -> str | None:
    if not proxy:
        return None
    try:
        ProxyValidator.validate_proxy_url(proxy)
    except Exception:
        raise InvalidProxyURL(INVALID_PROXY_MESSAGE) from None
    return proxy


def create_outbound_http_session(*, proxy: str | None = None, timeout_seconds: float = 10) -> aiohttp.ClientSession:
    """Create an HTTP client whose TLS changes cannot affect server sockets."""
    return aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(ssl=create_outbound_ssl_context()),
        timeout=aiohttp.ClientTimeout(total=timeout_seconds),
        proxy=checked_proxy_url(proxy),
    )
