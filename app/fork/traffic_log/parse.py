import re
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime

BLOCK_ROUTE = "BLOCK"
MAX_LABEL = 128
MAX_HOST = 255
MAX_INBOUND = 256
MAX_ROUTE = 128
MAX_USER_ID = 2**63 - 1
TAG_SEPARATORS = (" -> ", " >> ")
REFUSING_OUTBOUNDS = ("block", "reject")
SINGBOX_FLOW_CAP = 4096

ACCESS_LINE = re.compile(
    r"from \S+ accepted (?P<protocol>tcp|udp):"
    r"(?:\[(?P<host6>[^\]\s]+)\]|(?P<host>[^:\s\[\]]+)):(?P<port>\d{1,5}) "
    r"\[(?P<tags>[^\]]*)\] email: (?P<email>\S+)\s*$"
)


SINGBOX_INBOUND = re.compile(
    r"\[(?P<cid>\d+)\s[^\]]*\]\s+inbound/(?P<itype>[\w-]+)\[(?P<itag>[^\]]*)\]:\s+"
    r"\[(?P<user>[^\]]*)\]\s+inbound(?P<packet>\s+packet)?\s+connection\s+to\s+"
    r"(?:\[(?P<host6>[^\]\s]+)\]|(?P<host>[^:\s\[\]]+)):(?P<port>\d{1,5})"
)

SINGBOX_OUTBOUND = re.compile(r"\[(?P<cid>\d+)\s[^\]]*\]\s+outbound/(?P<otype>[\w-]+)\[(?P<otag>[^\]]*)\]")


class SingboxFlows:
    __slots__ = ("_cap", "_pending")

    def __init__(self, cap: int = SINGBOX_FLOW_CAP):
        self._pending: OrderedDict[str, tuple] = OrderedDict()
        self._cap = cap

    def opened(self, cid: str, flow: tuple) -> None:
        self._pending[cid] = flow
        self._pending.move_to_end(cid)
        while len(self._pending) > self._cap:
            self._pending.popitem(last=False)

    def closed(self, cid: str) -> tuple | None:
        return self._pending.pop(cid, None)

    def __len__(self) -> int:
        return len(self._pending)


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


def parse_singbox_line(line: str, node_id: int, at: datetime, flows: SingboxFlows) -> Event | None:
    opened = SINGBOX_INBOUND.search(line)
    if opened is not None:
        port = int(opened.group("port"))
        if port > 65535:
            return None
        inbound = (opened.group("itag") or opened.group("itype")).strip()[:MAX_INBOUND]
        if not inbound:
            return None
        host = (opened.group("host6") or opened.group("host"))[:MAX_HOST]
        protocol = "udp" if opened.group("packet") else "tcp"
        user_id, user_label = _split_email(opened.group("user").strip())
        flows.opened(opened.group("cid"), (user_id, user_label, inbound, host, port, protocol))
        return None

    closed = SINGBOX_OUTBOUND.search(line)
    if closed is None:
        return None
    flow = flows.closed(closed.group("cid"))
    if flow is None:
        return None
    user_id, user_label, inbound, host, port, protocol = flow
    outbound_type = closed.group("otype").strip().lower()
    route = ((closed.group("otag") or outbound_type).strip() or outbound_type).upper()[:MAX_ROUTE]
    return Event(
        at=at,
        user_id=user_id,
        user_label=user_label,
        node_id=node_id,
        inbound=inbound,
        host=host,
        port=port,
        protocol=protocol,
        route=route,
        refused=route == BLOCK_ROUTE or outbound_type in REFUSING_OUTBOUNDS,
    )
