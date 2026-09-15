import json, sqlite3, sys, urllib.request, urllib.error

BASE = "http://127.0.0.1:8001"
H = {"Content-Type": "application/json", "Authorization": "Bearer " + open("/root/dev/.filter_token").read().strip()}
TAG = "Shadowsocks Open"
OURS = {"enabled": True, "destOverride": ["http", "tls", "quic"], "routeOnly": False}
failures = []


def call(m, p, b=None):
    r = urllib.request.Request(BASE + p, method=m, data=json.dumps(b).encode() if b is not None else None, headers=H)
    try:
        with urllib.request.urlopen(r, timeout=180) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


def core_cfg():
    st, c = call("GET", "/api/core/1")
    cfg = c["config"]
    return c, (json.loads(cfg) if isinstance(cfg, str) else cfg)


def sniffing_of(tag):
    _, cfg = core_cfg()
    return next((i.get("sniffing") for i in cfg["inbounds"] if i.get("tag") == tag), "MISSING")


def set_sniffing(tag, value):
    core, cfg = core_cfg()
    for i in cfg["inbounds"]:
        if i.get("tag") == tag:
            if value is None:
                i.pop("sniffing", None)
            else:
                i["sniffing"] = value
    st, r = call("PUT", "/api/core/1?restart_nodes=false", {
        "name": core["name"], "config": cfg, "type": core.get("type"),
        "exclude_inbound_tags": core.get("exclude_inbound_tags") or [],
        "fallbacks_inbound_tags": core.get("fallbacks_inbound_tags") or []})
    assert st == 200, ("set_sniffing", st, r)


def overrides():
    db = sqlite3.connect("file:/root/dev/panel/db.sqlite3?mode=ro", uri=True)
    return [(r[0], json.loads(r[1]) if r[1] else None)
            for r in db.execute("select inbound_tag, original from content_filter_sniffing_overrides where core_id=1")]


def check(label, got, want):
    ok = got == want
    print("  %-58s %s" % (label, "OK" if ok else "FAIL  got=%r want=%r" % (got, want)))
    if not ok:
        failures.append(label)


st, profiles = call("GET", "/api/content-filter/profiles")
pid = profiles[0]["id"]


def apply_then_withdraw(tag):
    st, a = call("POST", "/api/content-filter/assignments",
                 {"profile_id": pid, "node_id": 5, "inbound_tag": tag, "is_enabled": True})
    assert st == 201, ("apply", st, a)
    during = sniffing_of(tag)
    own = overrides()
    st2, _ = call("DELETE", "/api/content-filter/assignments/%d" % a["id"])
    assert st2 == 204, ("withdraw", st2)
    return during, own, sniffing_of(tag), overrides()


print("=== case 1: operator's own sniffing that EQUALS our defaults must survive ===")
set_sniffing(TAG, dict(OURS))
check("precondition: operator sniffing == our defaults", sniffing_of(TAG), OURS)
during, own, after, own_after = apply_then_withdraw(TAG)
check("during filter: sniffing on", during, OURS)
check("during filter: override row records the original", [o for o in own if o[0] == TAG], [(TAG, OURS)])
check("after withdraw: operator's sniffing STILL PRESENT", after, OURS)
check("after withdraw: override row gone", [o for o in own_after if o[0] == TAG], [])

print("\n=== case 2: an inbound with NO sniffing ends with NO sniffing ===")
set_sniffing(TAG, None)
check("precondition: no sniffing", sniffing_of(TAG), None)
during, own, after, own_after = apply_then_withdraw(TAG)
check("during filter: sniffing on", during, OURS)
check("during filter: override records original = None", [o for o in own if o[0] == TAG], [(TAG, None)])
check("after withdraw: sniffing removed again", after, None)

print("\n=== case 3: a DIFFERENT custom sniffing is restored byte-for-byte ===")
custom = {"enabled": True, "destOverride": ["http"], "routeOnly": True}
set_sniffing(TAG, custom)
check("precondition: custom sniffing", sniffing_of(TAG), custom)
during, own, after, own_after = apply_then_withdraw(TAG)
check("during filter: replaced by ours", during, OURS)
check("after withdraw: custom sniffing restored exactly", after, custom)

print("\n=== case 4: applying twice does not double-record ===")
set_sniffing(TAG, None)
st, a = call("POST", "/api/content-filter/assignments", {"profile_id": pid, "node_id": 5, "inbound_tag": TAG, "is_enabled": True})
call("POST", "/api/content-filter/assignments/%d/apply" % a["id"])
call("POST", "/api/content-filter/assignments/%d/apply" % a["id"])
check("one override row after three applies", len([o for o in overrides() if o[0] == TAG]), 1)
call("DELETE", "/api/content-filter/assignments/%d" % a["id"])
check("cleanup: sniffing back to none", sniffing_of(TAG), None)

print("\n=== case 5: an operator edit WHILE the override is active restores the pre-filter original ===")
set_sniffing(TAG, None)
st, a = call("POST", "/api/content-filter/assignments", {"profile_id": pid, "node_id": 5, "inbound_tag": TAG, "is_enabled": True})
assert st == 201, ("apply", st, a)
midway = {"enabled": True, "destOverride": ["quic"], "routeOnly": True}
set_sniffing(TAG, midway)
check("operator edit landed while active", sniffing_of(TAG), midway)
call("DELETE", "/api/content-filter/assignments/%d" % a["id"])
check("withdraw restores what was there BEFORE the filter, not the mid-edit value", sniffing_of(TAG), None)

print("\n=== case 6: ownership lives in the database, not in process memory ===")
set_sniffing(TAG, None)
st, a = call("POST", "/api/content-filter/assignments", {"profile_id": pid, "node_id": 5, "inbound_tag": TAG, "is_enabled": True})
assert st == 201
check("override row is a persisted database row", [o[0] for o in overrides() if o[0] == TAG], [TAG])
call("DELETE", "/api/content-filter/assignments/%d" % a["id"])

print("\n=== case 7: an inbound that vanishes and comes back under the same tag ===")
set_sniffing(TAG, None)
st, a = call("POST", "/api/content-filter/assignments", {"profile_id": pid, "node_id": 5, "inbound_tag": TAG, "is_enabled": True})
assert st == 201
check("row recorded with original None", [o for o in overrides() if o[0] == TAG], [(TAG, None)])
core, cfg = core_cfg()
saved = next(i for i in cfg["inbounds"] if i.get("tag") == TAG)
cfg["inbounds"] = [i for i in cfg["inbounds"] if i.get("tag") != TAG]
st, _ = call("PUT", "/api/core/1?restart_nodes=false", {"name": core["name"], "config": cfg, "type": core.get("type"),
    "exclude_inbound_tags": core.get("exclude_inbound_tags") or [], "fallbacks_inbound_tags": core.get("fallbacks_inbound_tags") or []})
assert st == 200, ("remove inbound", st)
call("POST", "/api/content-filter/assignments/%d/apply" % a["id"])
check("row dropped once its inbound is gone", [o for o in overrides() if o[0] == TAG], [])
reborn = dict(saved); reborn["sniffing"] = {"enabled": True, "destOverride": ["http"], "routeOnly": True}
core, cfg = core_cfg(); cfg["inbounds"].append(reborn)
st, _ = call("PUT", "/api/core/1?restart_nodes=false", {"name": core["name"], "config": cfg, "type": core.get("type"),
    "exclude_inbound_tags": core.get("exclude_inbound_tags") or [], "fallbacks_inbound_tags": core.get("fallbacks_inbound_tags") or []})
assert st == 200, ("re-add inbound", st)
call("POST", "/api/content-filter/assignments/%d/apply" % a["id"])
check("re-added inbound records ITS OWN original, not the stale one", [o for o in overrides() if o[0] == TAG], [(TAG, reborn["sniffing"])])
call("DELETE", "/api/content-filter/assignments/%d" % a["id"])
check("withdraw restores the re-added inbound's own sniffing", sniffing_of(TAG), reborn["sniffing"])
set_sniffing(TAG, None)

print("\n=== the real filtered endpoint is untouched by all of this ===")
check("Shadowsocks TCP still has sniffing", sniffing_of("Shadowsocks TCP"), OURS)

print()
if failures:
    print("FAILED: %d check(s): %s" % (len(failures), failures))
    sys.exit(1)
print("ALL %s CHECKS PASSED" % "ownership")
