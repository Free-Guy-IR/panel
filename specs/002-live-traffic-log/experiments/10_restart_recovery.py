import json
import subprocess
import os
import sys
import time
import urllib.request
PANEL_ROOT = os.environ.get("PANEL_ROOT", "/root/dev/panel")

BASE = "http://127.0.0.1:8001"
TOKEN = open(os.environ.get("TOKEN_FILE", "/root/dev/.filter_token")).read().strip()
NODE_ID = 5
failures = []


def check(label, got, want):
    ok = got == want
    print("  %-62s %s" % (label, "OK" if ok else "FAIL got=%r want=%r" % (got, want)))
    if not ok:
        failures.append(label)


def api(path, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method)
    request.add_header("Authorization", "Bearer " + TOKEN)
    if data:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=90) as response:
        return json.loads(response.read() or b"null")


def node_state():
    for node in api("/api/traffic-log/status")["nodes"]:
        if node["node_id"] == NODE_ID:
            return node
    return None


def drive(port, hosts):
    for host in hosts:
        subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "--socks5-hostname", f"127.0.0.1:{port}", "--max-time", "12", f"https://{host}"],
            capture_output=True,
        )


def wait_for(predicate, limit=180, step=5):
    deadline = time.time() + limit
    while time.time() < deadline:
        value = node_state()
        if value and predicate(value):
            return value, time.time()
        time.sleep(step)
    return node_state(), None


subprocess.run(["bash", os.environ.get("PROXIES", os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxies.sh")), "up"], capture_output=True)
before = node_state()
deadline = time.time() + 90
while time.time() < deadline and (before is None or before["state"] != "collecting"):
    drive(10822, ["www.wikipedia.org"])
    time.sleep(5)
    before = node_state()
check("node is collecting before the test", before["state"], "collecting")
drive(10822, ["www.wikipedia.org"])
time.sleep(8)
baseline = node_state()
check("baseline traffic was captured", baseline["events"] > before["events"], True)
print("   baseline: lines=%s events=%s restreams=%s" % (baseline["lines"], baseline["events"], baseline.get("restreams")))

print("\n=== restarting core 1 underneath the collector (no panel restart) ===")
core = api("/api/core/1")
api(
    "/api/core/1?restart_nodes=true",
    "PUT",
    {
        "name": core["name"],
        "config": core["config"],
        "exclude_inbound_tags": core.get("exclude_inbound_tags") or [],
        "fallbacks_inbound_tags": core.get("fallbacks_inbound_tags") or [],
    },
)
restarted_at = time.time()
print("   core restarted at %.0f" % restarted_at)

time.sleep(20)
subprocess.run(["bash", os.environ.get("PROXIES", os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxies.sh")), "up"], capture_output=True)
after_restart = node_state()
print("   right after: lines=%s events=%s restreams=%s state=%s" % (
    after_restart["lines"], after_restart["events"], after_restart.get("restreams"), after_restart["state"]))

print("\n=== driving traffic every 10 s until the collector picks it up again ===")
target = after_restart["events"]
recovered = None
deadline = time.time() + 180
while time.time() < deadline:
    drive(10822, ["www.wikipedia.org", "www.google.com"])
    time.sleep(10)
    now = node_state()
    print("   +%3.0fs lines=%-5s events=%-5s restreams=%-3s state=%s" % (
        time.time() - restarted_at, now["lines"], now["events"], now.get("restreams"), now["state"]))
    if now["events"] > target:
        recovered = time.time() - restarted_at
        break

check("collection recovered WITHOUT a panel restart", recovered is not None, True)
if recovered is not None:
    print("   recovered %.0f s after the core restart" % recovered)
    check("recovery took under 120 s", recovered < 120, True)
    final = node_state()
    check("the reader reopened its stream at least once", (final.get("restreams") or 0) >= 1, True)

print()
if failures:
    print("FAILED %d check(s): %s" % (len(failures), failures))
    raise SystemExit(1)
print("ALL restart-recovery CHECKS PASSED")
