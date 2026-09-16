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
NODE_ID = int(os.environ.get("NODE_ID", "5"))
KIDS_PORT = 10821
PROBE_HOSTS = ("www.wikipedia.org", "www.google.com", "sub.khanacademy.org")
PROBE_ROUNDS = 2
CAPTURE_SECONDS = 25
SETTLE_SECONDS = 6
COLLECTING_DEADLINE = 45
ACCESS = re.compile(r"from \S+ accepted (?:tcp|udp):(?P<dest>.+):(?P<port>\d+) \[(?P<tags>[^\]]+)\] email: (?P<email>\S+)\s*$")
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
                        self.lines.append(text[5:].strip())
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


def port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


def node_state():
    st, body = call("GET", "/api/traffic-log/status")
    body = body if isinstance(body, dict) else {}
    entry = next((n for n in body.get("nodes", []) if n.get("node_id") == NODE_ID), {})
    return st, body, entry.get("state", "absent")


def wait_for_state(wanted, deadline):
    started = time.monotonic()
    state = None
    while time.monotonic() - started < deadline:
        if wanted == "collecting":
            wait_all([curl_via(KIDS_PORT, PROBE_HOSTS[0])])
        _, _, state = node_state()
        if state == wanted:
            return state, time.monotonic() - started
        time.sleep(2)
    return state, time.monotonic() - started


def capture(label):
    reader = SseReader("/api/node/%d/logs" % NODE_ID, CAPTURE_SECONDS + 10).start()
    check("%s: /api/node/%d/logs opened (HTTP 200)" % (label, NODE_ID), reader.status, 200)
    time.sleep(1)
    started = time.monotonic()
    for _ in range(PROBE_ROUNDS):
        for host in PROBE_HOSTS:
            wait_all([curl_via(KIDS_PORT, host)])
    while time.monotonic() - started < CAPTURE_SECONDS:
        lines = reader.data()
        if all(sum(1 for ln in lines if "accepted tcp:%s:" % host in ln) >= PROBE_ROUNDS for host in PROBE_HOSTS):
            break
        time.sleep(0.5)
    time.sleep(1)
    lines = reader.data()
    reader.stop()
    access = [ln for ln in lines if ACCESS.search(ln)]
    per_host = {host: sum(1 for ln in access if "accepted tcp:%s:" % host in ln) for host in PROBE_HOSTS}
    shaped = [ln for ln in lines if " accepted tcp:" in ln or " accepted udp:" in ln]
    print("    %s: %d data lines, %d access lines, per-probe %s" % (label, len(lines), len(access), per_host))
    if access:
        print("    %s: first access line -> %s" % (label, access[0]))
    check("%s: the viewer reported no error" % label, reader.error, None)
    check("%s: every accepted line matches the documented shape" % label, sorted(set(shaped) - set(access)), [])
    for host in PROBE_HOSTS:
        check("%s: the viewer shows %s" % (label, host), per_host[host] >= 1, True)
    return {"lines": len(lines), "access": len(access), "per_host": per_host, "samples": access[:2]}


def main():
    print("=== preconditions ===")
    check("socks proxy for demo-kids (%d) reachable" % KIDS_PORT, port_open(KIDS_PORT), True)
    st, body, state = node_state()
    check("GET /api/traffic-log/status -> 200", st, 200)
    check("collector available", body.get("available"), True)
    check("collection enabled", body.get("enabled"), True)
    if state != "collecting":
        state, waited = wait_for_state("collecting", COLLECTING_DEADLINE)
        print("    node %d reached state %s after %.0f s of probing" % (NODE_ID, state, waited))
    check("node %d is collecting" % NODE_ID, state, "collecting")
    if failures:
        finish()

    print("\n=== round 1: the per-node log viewer while the collector is ATTACHED ===")
    attached = capture("attached")

    print("\n=== detaching the collector (PUT /settings enabled=false) ===")
    st, paused = call("PUT", "/api/traffic-log/settings", {"enabled": False})
    paused = paused if isinstance(paused, dict) else {}
    check('PUT /settings {"enabled":false} -> 200', st, 200)
    check("status reports enabled false", paused.get("enabled"), False)
    check("node %d is no longer collecting" % NODE_ID, node_state()[2] in ("paused", "detached"), True)
    time.sleep(SETTLE_SECONDS)

    print("\n=== round 2: the same viewer while the collector is DETACHED ===")
    detached = capture("detached")

    print("\n=== re-attaching the collector ===")
    st, resumed = call("PUT", "/api/traffic-log/settings", {"enabled": True})
    resumed = resumed if isinstance(resumed, dict) else {}
    check('PUT /settings {"enabled":true} -> 200', st, 200)
    check("status reports enabled true", resumed.get("enabled"), True)
    state, waited = wait_for_state("collecting", COLLECTING_DEADLINE)
    print("    node %d reached state %s after %.0f s" % (NODE_ID, state, waited))
    check("node %d is collecting again" % NODE_ID, state, "collecting")

    probes = PROBE_ROUNDS * len(PROBE_HOSTS)
    deltas = {host: abs(attached["per_host"][host] - detached["per_host"][host]) for host in PROBE_HOSTS}
    print("\n=== SC-008: the per-node viewer attached versus detached ===")
    print("    attached: %s" % json.dumps(attached))
    print("    detached: %s" % json.dumps(detached))
    print("    per-probe difference: %s" % deltas)
    check("attached round captured every probe (%d)" % probes, attached["access"] >= probes, True)
    check("detached round captured every probe (%d)" % probes, detached["access"] >= probes, True)
    check("every probe was shown at least %d times while attached" % PROBE_ROUNDS, min(attached["per_host"].values()) >= PROBE_ROUNDS, True)
    check("every probe was shown at least %d times while detached" % PROBE_ROUNDS, min(detached["per_host"].values()) >= PROBE_ROUNDS, True)
    check("the two rounds differ by at most one access line per probe", max(deltas.values()) <= 1, True)


def finish():
    print()
    if failures:
        print("FAILED %d check(s): %s" % (len(failures), failures))
        sys.exit(1)
    print("ALL viewer-parity CHECKS PASSED")
    sys.exit(0)


main()
finish()
