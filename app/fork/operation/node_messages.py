import re

_TOKEN_WITH_AT = re.compile(r"(?<!\S)[^\s@]*@\S*")
_BARE_USER_PREFIX = re.compile(r"(?:^|(?<=['\"(\[<=,;]))[^\s:/@'\"=,;()\[\]<>]+:(?!//)")
_TELEGRAM_BOT_TOKEN = re.compile(r"(?<=/bot)\d+:[A-Za-z0-9_-]+")
_SCHEME_SEPARATOR = "://"
_MASK = "***@"


def _scrub_token(match: re.Match) -> str:
    token = match.group()
    last_at = token.rfind("@")
    separator = token.find(_SCHEME_SEPARATOR)
    if separator >= 0 and last_at >= separator + len(_SCHEME_SEPARATOR) + 1:
        kept = separator + len(_SCHEME_SEPARATOR)
        token = token[:kept] + _MASK + token[last_at + 1 :]
        last_at = kept + len(_MASK) - 1
    prefix = _BARE_USER_PREFIX.search(token)
    if prefix is not None and last_at > prefix.end():
        token = token[: prefix.start()] + _MASK + token[last_at + 1 :]
    return token


def scrub_credentials(text: object) -> str:
    return _TELEGRAM_BOT_TOKEN.sub("***", _TOKEN_WITH_AT.sub(_scrub_token, str(text)))


def scrub_and_truncate(text: object, limit: int, suffix: str = "") -> str:
    scrubbed = scrub_credentials(text)
    if len(scrubbed) <= limit:
        return scrubbed
    return scrubbed[: limit - len(suffix)] + suffix


def describe_error(error: BaseException) -> str:
    return scrub_credentials(f"{type(error).__name__}: {error}")
