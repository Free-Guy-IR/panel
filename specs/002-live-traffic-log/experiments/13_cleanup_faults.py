#!/usr/bin/env python3
import importlib.util
import os
import sys
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(HERE, "12_owner_only.py")

failures = []


def check(label, got, want):
    ok = got == want
    print("  %-66s %s" % (label, "OK" if ok else "FAIL got=%r want=%r" % (got, want)))
    if not ok:
        failures.append(label)


def load():
    spec = importlib.util.spec_from_file_location("owner_only_under_test", TARGET)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def surviving(module):
    status, payload = module.call("GET", "/api/admins?limit=500", timeout=20)
    if status != 200 or not isinstance(payload, dict):
        return None
    names = [a.get("username") for a in (payload.get("admins") or []) if isinstance(a, dict)]
    return module.ADMIN_NAME in names


def role_survives(module):
    role_id, listed = module.find_role_id()
    if not listed:
        return None
    return role_id is not None


print("=== fault 1: the creation response is lost after the server already committed it ===")
module = load()
real_call = module.call

module.created["role_attempted"] = True
status, role = real_call(
    "POST", "/api/admin-role", {"name": module.ROLE_NAME, "permissions": module.maximal_permissions()}
)
check("the role really was created on the panel", status in (200, 201), True)
module.created["admin_attempted"] = True
status, _ = real_call(
    "POST",
    "/api/admin",
    {"username": module.ADMIN_NAME, "password": module.PASSWORD, "role_id": role.get("id")},
)
check("the admin really was created on the panel", status in (200, 201), True)
print("    the ids stay unrecorded, which is exactly what a lost response leaves behind")
check("no role id was recorded", module.created["role"], None)
check("no admin id was recorded", module.created["admin"], None)

removed = module.cleanup()
check("cleanup removed the admin it never saw a response for", removed["admin"], True)
check("cleanup removed the role it never saw a response for", removed["role"], True)
check("the admin no longer exists on the panel", surviving(module), False)
check("the role no longer exists on the panel", role_survives(module), False)

print()
print("=== fault 2: deleting the admin keeps failing, the role must still be attempted ===")
module = load()
real_call = module.call
attempts = {"admin": 0, "role": 0}


def refusing_admin_delete(method, path, body=None, headers=None, timeout=30, stream=False):
    if method == "DELETE" and path.startswith("/api/admin/"):
        attempts["admin"] += 1
        raise urllib.error.URLError("simulated failure deleting the admin")
    if method == "DELETE" and path.startswith("/api/admin-role/"):
        attempts["role"] += 1
    return real_call(method, path, body, headers, timeout, stream)


module.created["role_attempted"] = True
status, role = real_call(
    "POST", "/api/admin-role", {"name": module.ROLE_NAME, "permissions": module.maximal_permissions()}
)
check("the role was created for the second fault", status in (200, 201), True)
module.created["role"] = role.get("id")
module.created["admin_attempted"] = True
status, _ = real_call(
    "POST",
    "/api/admin",
    {"username": module.ADMIN_NAME, "password": module.PASSWORD, "role_id": role.get("id")},
)
check("the admin was created for the second fault", status in (200, 201), True)

module.call = refusing_admin_delete
removed = module.cleanup()
check("cleanup reported the admin as NOT removed", removed["admin"], False)
check("cleanup still attempted the role deletion", attempts["role"] >= 1, True)
check("cleanup retried the admin deletion", attempts["admin"] >= 3, True)

module.call = real_call
final = module.cleanup()
check("a clean retry removes the admin", final["admin"], True)
check("nothing is left behind", surviving(module), False)

print()
if failures:
    print("FAILED %d check(s): %s" % (len(failures), failures))
    sys.exit(1)
print("ALL cleanup-fault CHECKS PASSED")
sys.exit(0)
