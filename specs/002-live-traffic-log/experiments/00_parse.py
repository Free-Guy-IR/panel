#!/usr/bin/env python3
import dataclasses
import os
import sys
from datetime import UTC, datetime

ROOT = os.environ.get("PANEL_ROOT", "/root/dev/panel")
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from app.fork.traffic_log.parse import parse_access_line  # noqa: E402

SOURCE = "1.2.3.4:6746"
AT = datetime(2026, 9, 15, 14, 35, 37, 412000, tzinfo=UTC)
NODE_ID = 5
failures = []


def check(label, got, want):
    ok = got == want
    print("  %-62s %s" % (label, "OK" if ok else "FAIL got=%r want=%r" % (got, want)))
    if not ok:
        failures.append(label)


def fields_of(event):
    if hasattr(event, "model_dump"):
        return event.model_dump()
    if dataclasses.is_dataclass(event):
        return dataclasses.asdict(event)
    if hasattr(event, "_asdict"):
        return event._asdict()
    if hasattr(event, "__dict__"):
        return dict(vars(event))
    return {n: getattr(event, n) for n in dir(event) if not n.startswith("_") and not callable(getattr(event, n))}


def access(dest, tags, email, proto="tcp", source=SOURCE):
    return "2026/09/15 14:35:37 from %s accepted %s:%s [%s] email: %s" % (source, proto, dest, tags, email)


def parse(line):
    return parse_access_line(line, NODE_ID, AT)


def expect_event(label, line, **want):
    ev = parse(line)
    if ev is None:
        check(label, None, "Event")
        return None
    for name, value in want.items():
        check("%s: %s" % (label, name), getattr(ev, name, "<missing attribute>"), value)
    return ev


print("=== verified lines from the test node (research.md §1) ===")
wiki = expect_event(
    "'->' DIRECT wikipedia",
    access("www.wikipedia.org:443", "Shadowsocks TCP -> DIRECT", "72"),
    host="www.wikipedia.org",
    port=443,
    protocol="tcp",
    inbound="Shadowsocks TCP",
    route="DIRECT",
    refused=False,
    user_id=72,
    node_id=NODE_ID,
    at=AT,
)
expect_event(
    "'>>' DIRECT google",
    access("www.google.com:443", "Shadowsocks TCP >> DIRECT", "72"),
    host="www.google.com",
    port=443,
    protocol="tcp",
    inbound="Shadowsocks TCP",
    route="DIRECT",
    refused=False,
    user_id=72,
)
expect_event(
    "'-> BLOCK' pornhub is refused",
    access("www.pornhub.com:443", "Shadowsocks TCP -> BLOCK", "72"),
    host="www.pornhub.com",
    port=443,
    protocol="tcp",
    route="BLOCK",
    refused=True,
    user_id=72,
)

print("\n=== shape variants ===")
expect_event(
    "udp line",
    access("8.8.8.8:53", "Shadowsocks TCP >> DIRECT", "72", proto="udp"),
    host="8.8.8.8",
    port=53,
    protocol="udp",
    refused=False,
    user_id=72,
)
expect_event(
    "IPv6 bracketed host",
    "2026/09/15 14:35:37 from %s accepted tcp:[2001:db8::1]:443 [Shadowsocks TCP >> DIRECT] email: 72" % SOURCE,
    host="2001:db8::1",
    port=443,
    protocol="tcp",
    user_id=72,
)
expect_event(
    "IPv6 source address does not confuse the parser",
    access("www.google.com:443", "Shadowsocks TCP >> DIRECT", "72", source="[2001:db8::9]:5555"),
    host="www.google.com",
    port=443,
    user_id=72,
)
expect_event(
    "tags with spaces preserved",
    access("www.google.com:443", "VLESS TCP REALITY -> Proxy Out NL", "72"),
    inbound="VLESS TCP REALITY",
    route="Proxy Out NL",
    refused=False,
)
expect_event(
    "non-numeric email",
    access("www.google.com:443", "Shadowsocks TCP >> DIRECT", "abc"),
    user_id=None,
    user_label="abc",
    host="www.google.com",
)
expect_event(
    "high port and numeric host",
    access("10.0.0.7:65535", "Shadowsocks TCP >> DIRECT", "73"),
    host="10.0.0.7",
    port=65535,
    user_id=73,
)

print("\n=== non-access lines ===")
check("warning line -> None", parse("2026/09/15 14:35:37 [Warning] core: something"), None)
check("info line -> None", parse("2026/09/15 14:35:37 [Info] [12345] app/proxyman/inbound: connection ends"), None)
check("empty line -> None", parse(""), None)
check(
    "access-like line without email -> None",
    parse("2026/09/15 14:35:37 from %s accepted tcp:www.google.com:443 [Shadowsocks TCP >> DIRECT]" % SOURCE),
    None,
)
check(
    "rejected connection line -> None",
    parse("2026/09/15 14:35:37 from %s rejected  proxy/shadowsocks: invalid request" % SOURCE),
    None,
)

print("\n=== privacy: the source address never reaches the event ===")
if wiki is not None:
    values = fields_of(wiki)
    print("    event fields: %s" % sorted(values))
    check("no attribute contains the source address", any(SOURCE in str(v) for v in values.values()), False)
    check("no attribute contains the source IP", any("1.2.3.4" in str(v) for v in values.values()), False)
    check("no attribute contains the source port", any("6746" in str(v) for v in values.values()), False)
else:
    check("no attribute contains the source address", "no event", "checked")

print("\n=== refused is always a real boolean, never null (contract guard) ===")
ROUTES = ("DIRECT", "BLOCK", "gemini-usa", "india_wireguard", "Some Route With Spaces")
seen_types = set()
for route in ROUTES:
    for protocol in ("tcp", "udp"):
        for arrow in ("->", ">>"):
            line = "2026/09/15 14:35:37 from 10.0.0.1:5555 accepted %s:example.test:443 [In Bound %s %s] email: 72" % (protocol, arrow, route)
            event = parse_access_line(line, 5, datetime.now(UTC))
            check("parsed %-5s %s %-22s" % (protocol, arrow, route), event is not None, True)
            if event is not None:
                seen_types.add(type(event.refused).__name__)
                check("  refused is a bool", isinstance(event.refused, bool), True)
                check("  refused matches the route", event.refused, route == "BLOCK")
check("only bool was ever produced", sorted(seen_types), ["bool"])

print()
if failures:
    print("FAILED %d check(s): %s" % (len(failures), failures))
    sys.exit(1)
print("ALL parse CHECKS PASSED")
