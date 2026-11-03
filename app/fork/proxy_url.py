import re
from urllib.parse import unquote

from yarl import URL

PROXY_URL_PATTERN = re.compile(
    r"(?P<scheme>http|https|socks4|socks5)://"
    r"((?P<user>[^\s:@\[\]\\]+):(?P<password>[^\s:@\[\]\\]+)@)?"
    r"(?P<host>[a-zA-Z0-9\.-]+)"
    r":(?P<port>\d{1,5})"
)
UNUSABLE_PROXY_URL = (
    "proxy_url cannot be used by the HTTP client as written: percent-encode reserved characters "
    "such as \\ [ ] / ? # in the user name and password"
)
PROXY_PORT_OUT_OF_RANGE = "proxy_url port must be between 1 and 65535, written with the digits 0-9"


def _decoded(value: str | None) -> str | None:
    return None if value is None else unquote(value)


def _written(value: str) -> re.Match:
    written = PROXY_URL_PATTERN.fullmatch(value)
    if written is None:
        raise ValueError(UNUSABLE_PROXY_URL)
    if not written["port"].isascii() or not 1 <= int(written["port"]) <= 65535:
        raise ValueError(PROXY_PORT_OUT_OF_RANGE)
    return written


def _parsed(value: str) -> URL | None:
    try:
        return URL(value)
    except ValueError:
        return None


def proxy_url_reaches_its_host(value: str | None) -> str | None:
    if not value:
        return value
    written = _written(value)
    parsed = _parsed(value)
    if (
        parsed is None
        or parsed.scheme != written["scheme"]
        or (parsed.raw_host or "").lower() != written["host"].lower()
        or parsed.explicit_port != int(written["port"])
        or parsed.user != _decoded(written["user"])
        or parsed.password != _decoded(written["password"])
    ):
        raise ValueError(UNUSABLE_PROXY_URL)
    return value
