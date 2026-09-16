#!/usr/bin/env python3
import json
import os
import random
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta

ROOT = os.environ.get("PANEL_ROOT", "/root/dev/panel")
BASE = os.environ.get("PANEL_URL", "http://127.0.0.1:8001")
TOKEN = open(os.environ.get("TOKEN_FILE", "/root/dev/.filter_token")).read().strip()
OWNER = {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"}
SUFFIX = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(6))
ROLE_NAME = "tl-maximal-role-" + SUFFIX
ADMIN_NAME = "tl-maximal-" + SUFFIX
PASSWORD = "Tl7" + "".join(random.choice(string.ascii_letters + string.digits) for _ in range(10)) + "Qz9!"

failures = []
created = {"admin": None, "role": None, "admin_attempted": False, "role_attempted": False}


def check(label, got, want):
    ok = got == want
    print("  %-66s %s" % (label, "OK" if ok else "FAIL got=%r want=%r" % (got, want)))
    if not ok:
        failures.append(label)


def call(method, path, body=None, headers=None, timeout=30, stream=False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, method=method, data=data, headers=headers or OWNER)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        try:
            if stream:
                return e.code, None
            raw = e.read().decode()
        finally:
            e.close()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw[:200]
    try:
        if stream:
            return resp.status, None
        raw = resp.read().decode()
        return resp.status, (json.loads(raw) if raw else None)
    finally:
        resp.close()


def login(username, password):
    data = urllib.parse.urlencode({"username": username, "password": password}).encode()
    req = urllib.request.Request(
        BASE + "/api/admin/token", data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]


def maximal_permissions():
    sys.path.insert(0, ROOT)
    from app.models.admin_role import RolePermissions

    permissions = {}
    for resource, field in RolePermissions.model_fields.items():
        group = field.annotation
        inner = next((arg for arg in getattr(group, "__args__", []) if hasattr(arg, "model_fields")), group)
        actions = getattr(inner, "model_fields", None)
        if not actions:
            continue
        permissions[resource] = {action: True for action in actions}
    return permissions


def window():
    now = datetime.now(UTC)
    return (now - timedelta(hours=1)).isoformat(), now.isoformat()


def restricted_endpoints():
    start, end = window()
    start, end = urllib.parse.quote(start), urllib.parse.quote(end)
    return [
        ("GET", "/api/traffic-log/status", None),
        ("GET", "/api/traffic-log/history?start=%s&end=%s" % (start, end), None),
        ("GET", "/api/traffic-log/summary?start=%s&end=%s" % (start, end), None),
        ("GET", "/api/traffic-log/live", None),
        ("PUT", "/api/traffic-log/settings", {"enabled": True}),
        ("POST", "/api/traffic-log/purge", {"older_than_hours": 9999}),
        ("GET", "/api/content-filter/catalog", None),
        ("GET", "/api/content-filter/targets", None),
        ("GET", "/api/content-filter/profiles", None),
        ("GET", "/api/content-filter/assignments", None),
        ("POST", "/api/content-filter/test", {"destination": "example.com"}),
        ("GET", "/api/node/inbounds/usage", None),
        ("GET", "/api/nodes/realtime_stats", None),
    ]


def find_role_id():
    if created["role"] is not None:
        return created["role"], True
    try:
        status, payload = call("GET", "/api/admin-roles?limit=200", timeout=20)
    except Exception as error:
        print("    listing roles to recover the temporary one failed: %r" % (error,))
        return None, False
    if status != 200 or not isinstance(payload, dict):
        return None, False
    for role in payload.get("roles") or []:
        if isinstance(role, dict) and role.get("name") == ROLE_NAME:
            created["role"] = role.get("id")
            return created["role"], True
    return None, True


def delete_admin():
    try:
        status, _ = call("DELETE", "/api/admin/%s" % ADMIN_NAME, timeout=20)
    except Exception as error:
        print("    deleting the temporary admin failed: %r" % (error,))
        return False
    return status in (200, 204, 404)


def delete_role():
    role_id, listed = find_role_id()
    if role_id is None:
        return listed
    try:
        status, _ = call("DELETE", "/api/admin-role/%s" % role_id, timeout=20)
    except Exception as error:
        print("    deleting the temporary role failed: %r" % (error,))
        return False
    if status in (200, 204, 404):
        created["role"] = None
        return True
    return False


def cleanup():
    removed = {"admin": not created["admin_attempted"], "role": not created["role_attempted"]}
    for attempt in range(3):
        if not removed["admin"]:
            removed["admin"] = delete_admin()
        if not removed["role"]:
            removed["role"] = delete_role()
        if removed["admin"] and removed["role"]:
            break
        if attempt < 2:
            time.sleep(2)
    if not removed["admin"]:
        print("    LEFTOVER: admin %s could not be deleted" % ADMIN_NAME)
    if not removed["role"]:
        print("    LEFTOVER: role %s (id %r) could not be confirmed deleted" % (ROLE_NAME, created["role"]))
    return removed


def report(removed):
    print()
    print("=== cleanup ===")
    check("the temporary admin was deleted", removed["admin"], True)
    check("the temporary role was deleted", removed["role"], True)
    if created["admin_attempted"] and removed["admin"]:
        status, _ = login(ADMIN_NAME, PASSWORD)
        check("the temporary admin can no longer log in", status in (401, 403), True)
    print()
    if failures:
        print("FAILED %d check(s): %s" % (len(failures), failures))
        return 1
    print("ALL owner-only CHECKS PASSED")
    return 0


def main():
    print("=== building a role that holds every ordinary permission but is not the owner ===")
    permissions = maximal_permissions()
    print("    resources granted: %s" % ", ".join(sorted(permissions)))
    check("the role grants nodes.stats", permissions.get("nodes", {}).get("stats"), True)
    check("the role grants nodes.logs", permissions.get("nodes", {}).get("logs"), True)
    check("the role grants settings.read", permissions.get("settings", {}).get("read"), True)
    check("the role grants settings.update", permissions.get("settings", {}).get("update"), True)

    created["role_attempted"] = True
    status, role = call("POST", "/api/admin-role", {"name": ROLE_NAME, "permissions": permissions})
    check("POST /api/admin-role -> 200/201", status in (200, 201), True)
    if status not in (200, 201):
        print("    response: %r" % (role,))
        return
    created["role"] = role.get("id") if isinstance(role, dict) else None
    check("the created role is not an owner role", bool(isinstance(role, dict) and role.get("is_owner")), False)

    created["admin_attempted"] = True
    status, admin = call(
        "POST", "/api/admin", {"username": ADMIN_NAME, "password": PASSWORD, "role_id": created["role"]}
    )
    check("POST /api/admin -> 200/201", status in (200, 201), True)
    if status not in (200, 201):
        print("    response: %r" % (admin,))
        return

    status, tokens = login(ADMIN_NAME, PASSWORD)
    check("the maximal admin can log in", status, 200)
    if status != 200:
        return
    loaded = {"Authorization": "Bearer " + tokens["access_token"], "Content-Type": "application/json"}

    endpoints = restricted_endpoints()

    print()
    print("=== every restricted endpoint must refuse an admin that is not the panel owner ===")
    for method, path, body in endpoints:
        status, _ = call(method, path, body, headers=loaded, timeout=20, stream=path.endswith("/live"))
        check("%-4s %-58s -> 403" % (method, path.split("?")[0]), status, 403)

    print()
    print("=== the same endpoints must still answer the panel owner ===")
    for method, path, body in endpoints:
        if method != "GET" or path.startswith("/api/traffic-log/live"):
            continue
        status, _ = call(method, path, body, headers=OWNER, timeout=20)
        check("%-4s %-58s -> 200" % (method, path.split("?")[0]), status, 200)

    print()
    print("=== a route the maximal admin may still use, so the role is proven functional ===")
    status, _ = call("GET", "/api/nodes/simple?all=true", headers=loaded, timeout=20)
    check("GET  /api/nodes/simple                                     -> 200", status, 200)


if __name__ == "__main__":
    try:
        main()
    finally:
        code = report(cleanup())
    sys.exit(code)
