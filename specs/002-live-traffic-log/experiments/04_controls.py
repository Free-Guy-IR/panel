#!/usr/bin/env python3
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.environ.get("PANEL_ROOT", "/root/dev/panel")
BASE = os.environ.get("PANEL_URL", "http://127.0.0.1:8001")
TOKEN = open(os.environ.get("TOKEN_FILE", "/root/dev/.filter_token")).read().strip()
H = {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"}
NODE_ID = int(os.environ.get("NODE_ID", "5"))
KIDS_PORT = 10821
SUBSCRIBER_QUEUE_SIZE = 1_000
FLOOD_EVENTS = int(os.environ.get("TL_FLOOD_EVENTS", "4000"))
FLOOD_WORKERS = int(os.environ.get("TL_FLOOD_WORKERS", "32"))
FLOOD_HOST = "127.0.0.1"
FLOOD_PORT = 9
SMALL_RCVBUF = 2048
DRAIN_SECONDS = 60
DROPPED_KEYS = ("dropped", "dropped_live")
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


def node_entry():
    st, body = call("GET", "/api/traffic-log/status")
    body = body if isinstance(body, dict) else {}
    entry = next((n for n in body.get("nodes", []) if n.get("node_id") == NODE_ID), {})
    return st, body, entry


def dropped_total(entry):
    return sum(int(entry.get(key) or 0) for key in DROPPED_KEYS if key in entry)


class RawSse:
    def __init__(self, path):
        parts = urllib.parse.urlsplit(BASE)
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SMALL_RCVBUF)
        self.sock.settimeout(10)
        self.sock.connect((parts.hostname, parts.port or 80))
        request = (
            "GET %s HTTP/1.1\r\n"
            "Host: %s\r\n"
            "Authorization: Bearer %s\r\n"
            "Accept: text/event-stream\r\n"
            "Connection: close\r\n\r\n" % (path, parts.netloc, TOKEN)
        )
        self.sock.sendall(request.encode())
        self.buffer = b""
        self.status_line = None
        self.messages = []

    def _consume(self):
        while b"\n" in self.buffer:
            line, _, self.buffer = self.buffer.partition(b"\n")
            text = line.decode("utf-8", "replace").strip()
            if self.status_line is None and text.startswith("HTTP/"):
                self.status_line = text
                continue
            if text.startswith("data:"):
                try:
                    self.messages.append(json.loads(text[5:].strip()))
                except ValueError:
                    continue

    def drain(self, seconds, until=None):
        deadline = time.monotonic() + seconds
        self.sock.settimeout(1.0)
        while time.monotonic() < deadline:
            try:
                chunk = self.sock.recv(4096)
            except TimeoutError:
                continue
            except OSError:
                break
            if not chunk:
                break
            self.buffer += chunk
            self._consume()
            if until is not None and any(until(m) for m in self.messages):
                break
        return self.messages

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


def socks_probe():
    sock = socket.create_connection(("127.0.0.1", KIDS_PORT), timeout=5)
    try:
        sock.sendall(b"\x05\x01\x00")
        sock.recv(2)
        sock.sendall(b"\x05\x01\x00\x01" + socket.inet_aton(FLOOD_HOST) + FLOOD_PORT.to_bytes(2, "big"))
        sock.settimeout(0.5)
        try:
            sock.recv(16)
        except (TimeoutError, OSError):
            pass
    finally:
        sock.close()


def flood(total, workers):
    counter = [0]
    lock = threading.Lock()

    def run(share):
        for _ in range(share):
            try:
                socks_probe()
            except OSError:
                continue
            with lock:
                counter[0] += 1

    share = max(1, total // workers)
    threads = [threading.Thread(target=run, args=(share,), daemon=True) for _ in range(workers)]
    started = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=240)
    return counter[0], time.monotonic() - started


def port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


def dropped_stage():
    print("=== stage 1: a subscriber that stops reading is told lines were dropped (FR-012) ===")
    check("socks proxy for demo-kids (%d) reachable" % KIDS_PORT, port_open(KIDS_PORT), True)
    st, body, before = node_entry()
    check("GET /status -> 200", st, 200)
    check("collector available", body.get("available"), True)
    check("collection enabled", body.get("enabled"), True)
    check("node %d is collecting" % NODE_ID, before.get("state"), "collecting")
    if failures:
        return
    print("    node %d before the flood: events=%s dropped=%s" % (NODE_ID, before.get("events"), dropped_total(before)))

    stream = RawSse("/api/traffic-log/live")
    print("    opened /api/traffic-log/live with SO_RCVBUF=%d and never read from it" % SMALL_RCVBUF)
    time.sleep(1)

    sent, elapsed = flood(FLOOD_EVENTS, FLOOD_WORKERS)
    print("    drove %d socks connections through %d in %.1f s" % (sent, KIDS_PORT, elapsed))
    check("the flood exceeded the subscriber queue (%d)" % SUBSCRIBER_QUEUE_SIZE, sent > SUBSCRIBER_QUEUE_SIZE, True)

    messages = stream.drain(DRAIN_SECONDS, until=lambda m: m.get("control") == "dropped")
    stream.close()
    controls = [m for m in messages if "control" in m]
    events = [m for m in messages if "control" not in m]
    dropped = [m for m in controls if m.get("control") == "dropped"]
    print("    raw stream answered %r" % stream.status_line)
    print("    read back %d events and %d control messages: %s" % (len(events), len(controls), controls[:5]))
    check("the live stream accepted the raw request", (stream.status_line or "").startswith("HTTP/1.1 200"), True)
    check("a dropped control message arrived", len(dropped) >= 1, True)
    check("the dropped control carries a positive count", all(isinstance(m.get("count"), int) and m["count"] > 0 for m in dropped) and bool(dropped), True)

    time.sleep(2)
    _, _, after = node_entry()
    print("    node %d after the flood: events=%s dropped=%s" % (NODE_ID, after.get("events"), dropped_total(after)))
    check(
        "the node recorded at least %d destination reports during the flood" % SUBSCRIBER_QUEUE_SIZE,
        int(after.get("events") or 0) - int(before.get("events") or 0) >= SUBSCRIBER_QUEUE_SIZE,
        True,
    )
    check("the node's dropped counter went up", dropped_total(after) > dropped_total(before), True)


async def unavailable_stage():
    from app.fork.traffic_log.collector import TrafficCollector
    from config import runtime_settings, server_settings
    from role import Role

    print("\n=== stage 2: every unsupported deployment shape refuses to collect ===")
    original_workers = server_settings.workers
    server_settings.workers = 2
    try:
        collector = TrafficCollector()
    finally:
        server_settings.workers = original_workers
    check("server_settings.workers restored", server_settings.workers, original_workers)
    check("a collector built with workers=2 is unavailable", collector.available, False)
    reason = collector.unavailable_reason or ""
    print("    reason given: %r" % reason)
    check("...and the reason is a readable sentence, not a bare label", len(reason.split()) >= 5, True)
    check("...and it names the web workers as the cause", "worker" in reason, True)

    for role in (Role.BACKEND, Role.NODE, Role.SCHEDULER):
        original_role = runtime_settings.role
        runtime_settings.role = role
        try:
            split = TrafficCollector()
        finally:
            runtime_settings.role = original_role
        check("role %s restored" % role.value, runtime_settings.role, original_role)
        print("    role %-9s -> available=%r reason=%r" % (role.value, split.available, split.unavailable_reason))
        check("a %s-role collector refuses to collect" % role.value, split.available, False)
        check("...and says why in a sentence", len((split.unavailable_reason or "").split()) >= 5, True)

    snapshot = collector.status()
    print("    status(): %s" % json.dumps(snapshot, default=str)[:300])
    check("status() reports available false", snapshot.get("available"), False)
    check("status() repeats the same reason", snapshot.get("reason"), collector.unavailable_reason)

    async with collector.subscribe() as queue:
        first = queue.get_nowait()
    check(
        "a subscriber is told the feed is unavailable, with the reason",
        first,
        {"control": "unavailable", "reason": collector.unavailable_reason},
    )

    await collector.start()
    check("start() attaches nothing while unavailable", collector.status().get("nodes"), [])
    await collector.ensure_attached(NODE_ID, None, "unavailable-node")
    check("ensure_attached() attaches nothing while unavailable", collector.is_attached(NODE_ID), False)


def main():
    import asyncio

    if os.environ.get("TL_RUN_FLOOD") == "1":
        dropped_stage()
    else:
        print("=== stage 1 skipped: the subscriber-overflow flood needs more load than this box can generate ===")
        print("    06_drop_paths.py proves the same requirement (FR-012) deterministically, in process.")
        print("    set TL_RUN_FLOOD=1 to attempt it anyway.")

    os.chdir(ROOT)
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    asyncio.run(unavailable_stage())

    print()
    if failures:
        print("FAILED %d check(s): %s" % (len(failures), failures))
        sys.exit(1)
    print("ALL control-message CHECKS PASSED")


main()
