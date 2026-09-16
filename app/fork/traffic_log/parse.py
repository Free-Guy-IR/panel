import re
from dataclasses import dataclass
from datetime import datetime

BLOCK_ROUTE = "BLOCK"
MAX_LABEL = 128
MAX_HOST = 255
MAX_INBOUND = 256
MAX_ROUTE = 128
MAX_USER_ID = 2**63 - 1
TAG_SEPARATORS = (" -> ", " >> ")

ACCESS_LINE = re.compile(
    r"from \S+ accepted (?P<protocol>tcp|udp):"
    r"(?:\[(?P<host6>[^\]\s]+)\]|(?P<host>[^:\s\[\]]+)):(?P<port>\d{1,5}) "
    r"\[(?P<tags>[^\]]*)\] email: (?P<email>\S+)\s*$"
)


@dataclass(slots=True, frozen=True)
class Event:
    at: datetime
    user_id: int | None
    user_label: str | None
    node_id: int
    inbound: str
    host: str
    port: int
    protocol: str
    route: str
    refused: bool


def _split_tags(tags: str) -> tuple[str, str] | None:
    for separator in TAG_SEPARATORS:
        inbound, found, route = tags.partition(separator)
        if found:
            inbound = inbound.strip()
            route = route.strip()
            if inbound and route:
                return inbound[:MAX_INBOUND], route[:MAX_ROUTE]
            return None
    return None


def _split_email(email: str) -> tuple[int | None, str | None]:
    if email.isascii() and email.isdigit():
        value = int(email)
        if value <= MAX_USER_ID:
            return value, None
    return None, email[:MAX_LABEL]


def parse_access_line(line: str, node_id: int, at: datetime) -> Event | None:
    match = ACCESS_LINE.search(line)
    if match is None:
        return None
    port = int(match.group("port"))
    if port > 65535:
        return None
    tags = _split_tags(match.group("tags"))
    if tags is None:
        return None
    inbound, route = tags
    user_id, user_label = _split_email(match.group("email"))
    host = match.group("host6") or match.group("host")
    return Event(
        at=at,
        user_id=user_id,
        user_label=user_label,
        node_id=node_id,
        inbound=inbound,
        host=host[:MAX_HOST],
        port=port,
        protocol=match.group("protocol"),
        route=route,
        refused=route == BLOCK_ROUTE,
    )
