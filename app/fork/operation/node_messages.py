import re

_TOKEN_WITH_AT = re.compile(r"(?<!\S)[^\s@]*@\S*")
_BARE_USER_PREFIX = re.compile(r"(?:^|(?<=['\"(\[<=,;]))[^\s:/@'\"=,;()\[\]<>]+:(?!//)")
_TELEGRAM_BOT_TOKEN = re.compile(r"(?<=/[Bb][Oo][Tt])\d+(?::|%3[Aa])[A-Za-z0-9_-]+")
_BARE_BOT_TOKEN = re.compile(r"(?<![A-Za-z0-9])((?:[Bb][Oo][Tt])?)\d{6,}(?::|%3[Aa])[A-Za-z0-9_-]{30,}")
_ENCODED_TOKEN = re.compile(r"(?<!\S)\S*%40\S*")
_ENCODED_SCHEME_SEPARATOR = "%3a%2f%2f"
_ENCODED_AT = "%40"
_PASSWORD_PAIR = re.compile(r"(?<![A-Za-z0-9_])((?:[Pp][Aa][Ss][Ss][Ww](?:[Oo][Rr])?[Dd]|[Pp][Ww][Dd])=)[^\s&;,'\"]+")
_PASSWORD_QUOTED = re.compile(
    r"""(?<![A-Za-z0-9])(["']?(?i:pass(?:wor|w)d|pwd)["']?[ \t]{0,8}[:=][ \t]{0,8})(["'])(?:(?!\2)[^\\\n]|\\.)+"""
)
_PASSWORD_COLON = re.compile(r"(?<![A-Za-z0-9])((?i:pass(?:wor|w)d|pwd)[ \t]{0,8}:[ \t]{0,8})([^\s&;,'\"]+)")
_SCHEME_SEPARATOR = "://"
_MASK = "***@"
_AUTHORITY = re.compile(r"(?<=://)[^\s/?#@]+")
_AUTHORITY_TRAILER = ".,;:)]}'\">"
_BASIC_AUTH = "BasicAuth("
_BASIC_AUTH_END = "encoding="


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


def _scrub_encoded_token(match: re.Match) -> str:
    token = match.group()
    lowered = token.lower()
    separator = lowered.find(_ENCODED_SCHEME_SEPARATOR)
    last_at = lowered.rfind(_ENCODED_AT)
    kept = separator + len(_ENCODED_SCHEME_SEPARATOR)
    if separator < 0 or last_at <= kept:
        return token
    return token[:kept] + "***" + token[last_at:]


def _scrub_authority(match: re.Match) -> str:
    authority = match.group()
    kept = authority.rstrip(_AUTHORITY_TRAILER)
    if kept.startswith("[") or ":" not in kept:
        return authority
    if kept.partition(":")[2].isdigit():
        return authority
    return "***" + authority[len(kept) :]


def _mask_basic_auth(text: str) -> str:
    start = text.find(_BASIC_AUTH)
    if start < 0:
        return text
    pieces = []
    position = 0
    while start >= 0:
        body = start + len(_BASIC_AUTH)
        line_end = text.find("\n", body)
        if line_end < 0:
            line_end = len(text)
        end = text.rfind(_BASIC_AUTH_END, body, line_end)
        pieces.append(text[position:body])
        if end < 0:
            pieces.append("***")
            position = line_end
        else:
            pieces.append("***, ")
            position = end
        start = text.find(_BASIC_AUTH, position)
    pieces.append(text[position:])
    return "".join(pieces)


def _mask_password_colon(match: re.Match) -> str:
    value = match.group(2)
    if value.isalpha() and value.islower():
        return match.group()
    return match.group(1) + "***"


def scrub_credentials(text: object) -> str:
    scrubbed = _TOKEN_WITH_AT.sub(_scrub_token, _mask_basic_auth(str(text)))
    scrubbed = _ENCODED_TOKEN.sub(_scrub_encoded_token, _AUTHORITY.sub(_scrub_authority, scrubbed))
    scrubbed = _BARE_BOT_TOKEN.sub(r"\1***", _TELEGRAM_BOT_TOKEN.sub("***", scrubbed))
    lowered = scrubbed.casefold()
    if "pass" in lowered or "pwd" in lowered:
        scrubbed = _PASSWORD_COLON.sub(_mask_password_colon, _PASSWORD_QUOTED.sub(r"\1\2***", scrubbed))
    return _PASSWORD_PAIR.sub(r"\1***", scrubbed)


def scrub_and_truncate(text: object, limit: int, suffix: str = "") -> str:
    scrubbed = scrub_credentials(text)
    if len(scrubbed) <= limit:
        return scrubbed
    return scrubbed[: limit - len(suffix)] + suffix


def describe_error(error: BaseException) -> str:
    return scrub_credentials(f"{type(error).__name__}: {error}")
