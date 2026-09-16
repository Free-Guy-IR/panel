#!/usr/bin/env python3
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = os.environ.get("PANEL_URL", "http://127.0.0.1:8001")
TOKEN = open(os.environ.get("TOKEN_FILE", "/root/dev/.filter_token")).read().strip()
H = {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"}
NODE_ID = 5
NODE_NAME = "filter-test-xray"
KIDS = {"username": "demo-kids", "user_id": 72, "port": 10821}
OPEN = {"username": "demo-open", "user_id": 73, "port": 10822}
SOURCE_PATTERN = re.compile(r" from (?P<source>\[[^\]]+\]|[^\s\[\]]+):\d+ accepted ")
EVENT_FIELDS = ("at", "user_id", "username", "node_id", "node", "inbound", "host", "port", "protocol", "route", "refused")
STAGE_DEADLINE = 40
FEED_LIMIT = 3.0
failures = []


def check(label, got, want):
    ok = got == want
    print("  %-62s %s" % (label, "OK" if ok else "FAIL got=%r want=%r" % (got, want)))
    if not ok:
        failures.append(label)


def call(method, path, body=None, headers=H, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw[:300]


class SseReader:
    def __init__(self, path, deadline, headers=H):
        self.path = path
        self.deadline = deadline
        self.headers = headers
        self.lines = []
        self.lock = threading.Lock()
        self.status = None
        self.error = None
        self.resp = None
        self.stopped = threading.Event()
        self.opened = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()
        self.opened.wait(10)
        return self

    def _run(self):
        req = urllib.request.Request(BASE + self.path, headers=self.headers)
        try:
            self.resp = urllib.request.urlopen(req, timeout=self.deadline)
            self.status = self.resp.status
        except urllib.error.HTTPError as e:
            self.status = e.code
            self.error = e.read().decode()[:300]
            self.opened.set()
            return
        except Exception as e:
            self.error = repr(e)
            self.opened.set()
            return
        self.opened.set()
        started = time.monotonic()
        try:
            for raw in self.resp:
                if self.stopped.is_set() or time.monotonic() - started > self.deadline:
                    break
                text = raw.decode("utf-8", "replace").rstrip("\r\n")
                if text.startswith("data:"):
                    with self.lock:
                        self.lines.append((time.monotonic(), text[5:].strip()))
        except Exception as e:
            if not self.stopped.is_set():
                self.error = repr(e)
        finally:
            self._close()

    def _close(self):
        try:
            self.resp.fp.raw._sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self.resp.close()
        except Exception:
            pass

    def stop(self):
        self.stopped.set()
        self._close()
        self.thread.join(3)

    def data(self):
        with self.lock:
            return list(self.lines)

    def events(self, since=0):
        out = []
        for ts, text in self.data()[since:]:
            try:
                out.append((ts, json.loads(text)))
            except ValueError:
                continue
        return out


def curl_via(port, host):
    return subprocess.Popen(
        ["curl", "-s", "-o", "/dev/null", "--socks5-hostname", "127.0.0.1:%d" % port, "--max-time", "12", "https://" + host],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def wait_all(procs, timeout=15):
    for p in procs:
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()


def wait_event(reader, predicate, since, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for ts, ev in reader.events(since):
            if "control" not in ev and predicate(ev):
                return ts, ev
        time.sleep(0.05)
    return None, None


def source_forms(raw_source):
    stripped = raw_source.strip()
    forms = {stripped}
    if stripped.startswith("[") and stripped.endswith("]"):
        forms.add(stripped[1:-1])
    else:
        forms.add("[%s]" % stripped)
    return {form for form in forms if form}


def port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


def node_entry(status):
    return next((n for n in status.get("nodes", []) if n.get("node_id") == NODE_ID), None)


def finish():
    print()
    if failures:
        print("FAILED %d check(s): %s" % (len(failures), failures))
        sys.exit(1)
    print("ALL live-e2e CHECKS PASSED")
    sys.exit(0)


print("=== preconditions ===")
check("socks proxy for demo-kids (10821) reachable", port_open(KIDS["port"]), True)
check("socks proxy for demo-open (10822) reachable", port_open(OPEN["port"]), True)
if failures:
    finish()

print("\n=== stage 0: collector status (poll up to 60 s) ===")
available = None
state = None
last_status = None
t0 = time.monotonic()
while time.monotonic() - t0 < 60:
    wait_all([curl_via(KIDS["port"], "www.wikipedia.org")])
    st, body = call("GET", "/api/traffic-log/status")
    if st == 200 and isinstance(body, dict):
        last_status = body
        available = body.get("available")
        entry = node_entry(body)
        state = entry.get("state") if entry else "absent"
        if available and state == "collecting":
            break
    time.sleep(2)
print("    status after %.1f s: %s" % (time.monotonic() - t0, json.dumps(last_status)[:400]))
check("status: available true", available, True)
check("status: node 5 state collecting", state, "collecting")
if state != "collecting":
    finish()

print("\n=== stage 1: warm the identity cache (one connection per user, then a flush) ===")
wait_all([curl_via(KIDS["port"], "www.wikipedia.org"), curl_via(OPEN["port"], "www.wikipedia.org")])
time.sleep(7)

print("\n=== stage 2: live feed + raw viewer parity ===")
live = SseReader("/api/traffic-log/live", STAGE_DEADLINE).start()
raw = SseReader("/api/node/%d/logs" % NODE_ID, STAGE_DEADLINE).start()
check("live stream opened (HTTP 200)", live.status, 200)
check("raw node log stream opened (HTTP 200)", raw.status, 200)
time.sleep(1)

plan = [
    (KIDS, "www.wikipedia.org"),
    (KIDS, "www.google.com"),
    (KIDS, "www.pornhub.com"),
    (OPEN, "www.wikipedia.org"),
    (OPEN, "www.pornhub.com"),
]
stage_start = len(live.data())
procs = []
observed = {}
for who, host in plan:
    since = len(live.data())
    started = time.monotonic()
    procs.append(curl_via(who["port"], host))
    ts, ev = wait_event(live, lambda e: e.get("user_id") == who["user_id"] and e.get("host") == host, since, 8)
    delta = (ts - started) if ts else None
    observed[(who["username"], host)] = (delta, ev)
    print(
        "    %-10s %-18s %s"
        % (who["username"], host, ("event after %.2f s  route=%s refused=%s" % (delta, ev.get("route"), ev.get("refused"))) if ev else "NO EVENT within 8 s")
    )

for (username, host), (delta, ev) in observed.items():
    label = "%s %s event within %.0f s" % (username, host, FEED_LIMIT)
    if delta is None:
        check(label, "no event", "<= %.0f s" % FEED_LIMIT)
    else:
        check("%s (%.2f s)" % (label, delta), delta <= FEED_LIMIT, True)

kids_porn = observed[("demo-kids", "www.pornhub.com")][1] or {}
kids_wiki = observed[("demo-kids", "www.wikipedia.org")][1] or {}
open_porn = observed[("demo-open", "www.pornhub.com")][1] or {}
check("demo-kids pornhub refused true", kids_porn.get("refused"), True)
check("demo-kids pornhub route BLOCK", kids_porn.get("route"), "BLOCK")
check("demo-kids wikipedia refused false", kids_wiki.get("refused"), False)
check("demo-open pornhub refused false (unrestricted config)", open_porn.get("refused"), False)

events = [ev for _, ev in live.events(stage_start) if "control" not in ev]
controls = [ev for _, ev in live.events(stage_start) if "control" in ev]
print("    %d events, %d control messages in this stage: %s" % (len(events), len(controls), controls[:5]))
kids_events = [e for e in events if e.get("user_id") == KIDS["user_id"]]
open_events = [e for e in events if e.get("user_id") == OPEN["user_id"]]
check("at least one demo-open event", len(open_events) >= 1, True)
check("every demo-open event carries username demo-open", all(e.get("username") == "demo-open" for e in open_events), True)
check("every demo-kids event carries username demo-kids", all(e.get("username") == "demo-kids" for e in kids_events), True)
missing = sorted({f for e in events for f in EVENT_FIELDS if f not in e})
check("every event has all contract fields", missing, [])
check("every event names node 5 as %s" % NODE_NAME, all(e.get("node") == NODE_NAME for e in events if e.get("node_id") == NODE_ID), True)
demo_events = kids_events + open_events
check("every demo event port is 443", all(e.get("port") == 443 for e in demo_events), True)
check("every demo event protocol is tcp", all(e.get("protocol") == "tcp" for e in demo_events), True)
check("no event carries a source-address field", any("from" in e or "source" in e or "src" in e for e in events), False)
observed_sources = set()
for _, text in raw.data():
    m = SOURCE_PATTERN.search(text)
    if not m:
        continue
    for form in source_forms(m.group("source")):
        observed_sources.add(form)
print("    source addresses the raw node log actually carried: %s" % (sorted(observed_sources) or "<none seen>"))
check("the raw log did carry at least one source address to test against", len(observed_sources) >= 1, True)
check(
    "no event value contains any observed client source address",
    any(src in str(v) for src in observed_sources for e in events for v in e.values()),
    False,
)

hosts = sorted({host for _, host in plan})
deadline = time.monotonic() + 5
while time.monotonic() < deadline:
    lines = [text for _, text in raw.data()]
    if all(any("accepted tcp:%s:" % h in ln for ln in lines) for h in hosts):
        break
    time.sleep(0.2)
lines = [text for _, text in raw.data()]
print("    raw viewer received %d data lines, %d access lines" % (len(lines), sum(1 for ln in lines if "accepted tcp:" in ln)))
for h in hosts:
    check("raw viewer still shows accepted tcp:%s" % h, any("accepted tcp:%s:" % h in ln for ln in lines), True)
check("raw viewer stream reported no error", raw.error, None)
check("live stream reported no error", live.error, None)

wait_all(procs)
live.stop()
raw.stop()

print("\n=== stage 3: exact-username filter (username=demo-open) ===")
flt = SseReader("/api/traffic-log/live?username=demo-open", 20).start()
check("filtered live stream opened (HTTP 200)", flt.status, 200)
time.sleep(1)
procs = [
    curl_via(KIDS["port"], "www.google.com"),
    curl_via(OPEN["port"], "www.wikipedia.org"),
    curl_via(KIDS["port"], "www.wikipedia.org"),
    curl_via(OPEN["port"], "www.google.com"),
]
wait_event(flt, lambda e: True, 0, 10)
time.sleep(4)
events = [ev for _, ev in flt.events() if "control" not in ev]
print("    filtered feed delivered %d events: %s" % (len(events), [(e.get("username"), e.get("host")) for e in events][:8]))
check("filtered feed: at least one event arrived", len(events) >= 1, True)
check("filtered feed: every event is demo-open", all(e.get("username") == "demo-open" and e.get("user_id") == OPEN["user_id"] for e in events), True)
check("filtered feed: no demo-kids event", any(e.get("user_id") == KIDS["user_id"] or e.get("username") == "demo-kids" for e in events), False)
wait_all(procs)
flt.stop()

finish()
