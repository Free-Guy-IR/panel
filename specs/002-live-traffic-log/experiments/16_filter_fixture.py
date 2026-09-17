#!/usr/bin/env python3
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("PANEL_URL", "http://127.0.0.1:8001")
TOKEN = open(os.environ.get("TOKEN_FILE", "/root/dev/.filter_token")).read().strip()
H = {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"}
PROFILE_NAME = "Kids"
NODE_ID = 5
INBOUND_TAG = "Shadowsocks TCP"
CATEGORIES = ["adult", "instagram", "pglist-42", "pglist-52", "tiktok"]
ALLOW_LIST = ["wikipedia.org", "*.khanacademy.org"]
BLOCK_LIST = ["=example.org"]
NOTE = "restricted profile for the demo"
ENFORCE_DEADLINE = 120
VERDICTS = {"www.pornhub.com": "BLOCK", "www.wikipedia.org": "DIRECT"}
failures = []
changes = []


def check(label, got, want):
    ok = got == want
    print("  %-62s %s" % (label, "OK" if ok else "FAIL got=%r want=%r" % (got, want)))
    if not ok:
        failures.append(label)


def call(method, path, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, method=method, data=data, headers=H)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, raw[:300]


def catalog_keys():
    st, body = call("GET", "/api/content-filter/catalog")
    if st != 200 or not isinstance(body, dict):
        return st, set()
    keys = set()
    for group in body.get("groups", []):
        keys.add(group.get("key"))
        for service in group.get("services", []):
            keys.add(service.get("key"))
    for entry in body.get("protection", []):
        keys.add(entry.get("key"))
    for group in body.get("lists", []):
        keys.add(group.get("key"))
        for entry in group.get("entries", []):
            keys.add(entry.get("key"))
    return st, {key for key in keys if key}


print("=== the suite provisions its own content-filter fixture ===")
status, known = catalog_keys()
check("catalog -> 200", status, 200)
missing = [key for key in CATEGORIES if key not in known]
check("every category the suite needs exists in the catalog", missing, [])
if failures:
    print("\nFAILED %d check(s): %s" % (len(failures), failures))
    sys.exit(1)

status, profiles = call("GET", "/api/content-filter/profiles")
check("profiles -> 200", status, 200)
if failures:
    print("\nFAILED %d check(s): %s" % (len(failures), failures))
    sys.exit(1)
profiles = profiles if isinstance(profiles, list) else []
existing = next((p for p in profiles if p.get("name") == PROFILE_NAME), None)

if existing is None:
    payload = {
        "name": PROFILE_NAME,
        "categories": CATEGORIES,
        "allow_list": ALLOW_LIST,
        "block_list": BLOCK_LIST,
        "strict_mode": True,
        "note": NOTE,
    }
    status, profile = call("POST", "/api/content-filter/profiles", payload)
    check("the missing profile was created -> 201", status, 201)
    changes.append("created the profile")
else:
    print("    the profile as the panel holds it now: categories=%s allow=%s block=%s strict=%s" % (
        sorted(existing.get("categories") or []),
        sorted(existing.get("allow_list") or []),
        sorted(existing.get("block_list") or []),
        existing.get("strict_mode"),
    ))
    payload = {
        "name": PROFILE_NAME,
        "categories": sorted(set(existing.get("categories") or []) | set(CATEGORIES)),
        "allow_list": sorted(set(existing.get("allow_list") or []) | set(ALLOW_LIST)),
        "block_list": sorted(set(existing.get("block_list") or []) | set(BLOCK_LIST)),
        "strict_mode": True,
        "note": existing.get("note") or NOTE,
    }
    drift = {
        field
        for field in ("categories", "allow_list", "block_list")
        if sorted(existing.get(field) or []) != payload[field]
    }
    if existing.get("strict_mode") is not True:
        drift.add("strict_mode")
    if drift:
        status, profile = call("PUT", "/api/content-filter/profiles/%d" % existing["id"], payload)
        check("the profile was topped up -> 200", status, 200)
        changes.append("added what was missing to " + ", ".join(sorted(drift)))
    else:
        profile = existing

profile = profile if isinstance(profile, dict) else {}
check("the profile carries every category the suite needs", sorted(set(CATEGORIES) - set(profile.get("categories") or [])), [])
check("the profile is strict", profile.get("strict_mode"), True)

status, assignments = call("GET", "/api/content-filter/assignments")
check("assignments -> 200", status, 200)
assignments = assignments if isinstance(assignments, list) else []
mine = next(
    (a for a in assignments if a.get("node_id") == NODE_ID and a.get("inbound_tag") == INBOUND_TAG),
    None,
)
if mine is None:
    status, mine = call(
        "POST",
        "/api/content-filter/assignments",
        {"profile_id": profile.get("id"), "node_id": NODE_ID, "inbound_tag": INBOUND_TAG, "is_enabled": True},
    )
    check("the missing assignment was created -> 201", status, 201)
    changes.append("created the assignment")
    mine = mine if isinstance(mine, dict) else {}
elif mine.get("profile_id") != profile.get("id") or not mine.get("is_enabled"):
    status, mine = call(
        "POST",
        "/api/content-filter/assignments",
        {"profile_id": profile.get("id"), "node_id": NODE_ID, "inbound_tag": INBOUND_TAG, "is_enabled": True},
    )
    check("the assignment was pointed back at the profile", status in (200, 201), True)
    changes.append("repointed the assignment")
    mine = mine if isinstance(mine, dict) else {}

status, applied = call("POST", "/api/content-filter/assignments/%d/apply" % mine.get("id", 0))
check("apply -> 200", status, 200)

deadline = time.monotonic() + ENFORCE_DEADLINE
enforced = None
last_error = None
while time.monotonic() < deadline:
    status, assignments = call("GET", "/api/content-filter/assignments")
    current = next((a for a in (assignments or []) if a.get("id") == mine.get("id")), None) or {}
    enforced, last_error = current.get("enforced"), current.get("last_error")
    if enforced is True and not last_error:
        break
    time.sleep(3)
check("the assignment reports itself enforced", enforced, True)
check("the assignment carries no error", last_error, None)

for domain, expected in sorted(VERDICTS.items()):
    status, verdict = call(
        "POST",
        "/api/content-filter/test",
        {"node_id": NODE_ID, "inbound_tag": INBOUND_TAG, "domain": domain},
    )
    verdict = verdict if isinstance(verdict, dict) else {}
    check("%s routes to %s before any traffic runs" % (domain, expected), verdict.get("outbound"), expected)

print()
print("    fixture changes this run: %s" % (", ".join(changes) if changes else "none, the panel already matched"))
if failures:
    print("FAILED %d check(s): %s" % (len(failures), failures))
    sys.exit(1)
print("ALL filter-fixture CHECKS PASSED")
sys.exit(0)
