import aiohttp
from aiohttp_socks import ProxyConnector

SOCKS_PROXY_SCHEMES = frozenset({"socks4", "socks5"})


def is_socks_proxy_url(proxy: str | None) -> bool:
    return bool(proxy) and proxy.split("://", 1)[0].lower() in SOCKS_PROXY_SCHEMES


def outbound_connector(proxy: str | None, ssl_context) -> tuple[aiohttp.BaseConnector, str | None]:
    if is_socks_proxy_url(proxy):
        return ProxyConnector.from_url(proxy, ssl=ssl_context, rdns=True), None
    return aiohttp.TCPConnector(ssl=ssl_context), proxy
