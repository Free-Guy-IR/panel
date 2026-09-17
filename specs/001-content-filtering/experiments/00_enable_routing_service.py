import json, urllib.request, urllib.error

BASE = "http://127.0.0.1:8001"
TOKEN = open("/root/dev/.filter_token").read().strip()
H = {"Content-Type": "application/json", "Authorization": "Bearer " + TOKEN}


def call(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers=H)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:500]


st, core = call("GET", "/api/core/1")
print("  GET core 1 -> %s" % st)
cfg = core["config"] if isinstance(core, dict) else None
if cfg is None:
    print("  could not read config:", str(core)[:200]); raise SystemExit(1)
if isinstance(cfg, str):
    cfg = json.loads(cfg)
print("  current api section: %s" % cfg.get("api"))

cfg["api"] = {"services": ["RoutingService"]}

body = {"name": core.get("name", "Default Core Config"), "config": cfg,
        "exclude_inbound_tags": core.get("exclude_inbound_tags") or [],
        "fallbacks_inbound_tags": core.get("fallbacks_inbound_tags") or []}
st, res = call("PUT", "/api/core/1?restart_nodes=true", body)
print("  PUT core 1 -> %s" % st)
if st >= 400:
    print("   ", str(res)[:400])
else:
    newcfg = res.get("config") if isinstance(res, dict) else None
    if isinstance(newcfg, str):
        newcfg = json.loads(newcfg)
    print("  api section now: %s" % (newcfg or {}).get("api"))
