import asyncio
import json
import subprocess
import os
import sys
import urllib.request
from datetime import UTC, datetime, timedelta

PANEL_ROOT = os.environ.get("PANEL_ROOT", "/root/dev/panel")

sys.path.insert(0, PANEL_ROOT)

BASE = "http://127.0.0.1:8001"
TOKEN = open(os.environ.get("TOKEN_FILE", "/root/dev/.filter_token")).read().strip()
failures = []


def check(label, got, want):
    ok = got == want
    print("  %-64s %s" % (label, "OK" if ok else "FAIL got=%r want=%r" % (got, want)))
    if not ok:
        failures.append(label)


def api(path, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method)
    request.add_header("Authorization", "Bearer " + TOKEN)
    if data:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read() or b"null")


def seed(rows, hours_old, tag):
    import sqlite3

    db = sqlite3.connect(os.path.join(PANEL_ROOT, "db.sqlite3"), timeout=60)
    when = datetime.now(UTC) - timedelta(hours=hours_old)
    stamp = when.strftime("%Y-%m-%d %H:%M:%S.%f")
    ids = []
    for index in range(rows):
        cur = db.execute(
            "insert into traffic_log_records (bucket_start,user_id,user_label,node_id,inbound_tag,host,port,protocol,refused,route,first_seen,last_seen,hits)"
            " values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (stamp, 72, None, 5, "MP Probe", "%s-%dh-%d.test" % (tag, hours_old, index), 443, "tcp", 0, "DIRECT", stamp, stamp, 1),
        )
        ids.append(cur.lastrowid)
    db.commit()
    db.close()
    return ids


def alive(ids):
    import sqlite3

    db = sqlite3.connect("file:%s?mode=ro" % os.path.join(PANEL_ROOT, "db.sqlite3"), uri=True)
    marks = ",".join("?" * len(ids))
    n = db.execute("select count(*) from traffic_log_records where id in (%s)" % marks, ids).fetchone()[0]
    db.close()
    return n


def drop(ids):
    import sqlite3

    db = sqlite3.connect(os.path.join(PANEL_ROOT, "db.sqlite3"), timeout=60)
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        db.execute("delete from traffic_log_records where id in (%s)" % ",".join("?" * len(chunk)), chunk)
    db.commit()
    db.close()


original = api("/api/traffic-log/status")["retention_hours"]
print("=== the API sets retention in the PANEL process ===")
api("/api/traffic-log/settings", "PUT", {"retention_hours": 3})
check("the panel now reports 3 hours", api("/api/traffic-log/status")["retention_hours"], 3)

print("\n=== a SEPARATE process whose collector never started must still honour it ===")
from app.fork.jobs.traffic_log_purge import purge_traffic_log
from app.fork.traffic_log import collector


check("this process's collector never started", collector._state_loaded, False)
check("its cached retention is the bare default", collector.retention_hours, 48)
resolved = asyncio.run(collector.effective_retention_hours())
check("effective_retention_hours() reads the database instead", resolved, 3)

aged = seed(25, 5, "mp-aged")
fresh = seed(25, 1, "mp-fresh")
check("seeded both ages", alive(aged + fresh), 50)
asyncio.run(purge_traffic_log())
check("rows older than the API-set 3h window were purged", alive(aged), 0)
check("rows inside that window survived", alive(fresh), 25)

print("\n=== once the collector HAS loaded state, the cached value is used ===")
collector.retention_hours = 3
collector._state_loaded = True
api("/api/traffic-log/settings", "PUT", {"retention_hours": 12})
cached = asyncio.run(collector.effective_retention_hours())
check("a started collector trusts its own cache", cached, 3)
collector._state_loaded = False
refreshed = asyncio.run(collector.effective_retention_hours())
check("an unstarted one re-reads and sees 12", refreshed, 12)

print("\n=== and the running panel itself already knows about the change ===")
check("the panel process reports 12", api("/api/traffic-log/status")["retention_hours"], 12)

drop(fresh)
api("/api/traffic-log/settings", "PUT", {"retention_hours": original})
check("retention restored", api("/api/traffic-log/status")["retention_hours"], original)

print()
if failures:
    print("FAILED %d check(s): %s" % (len(failures), failures))
    raise SystemExit(1)
print("ALL multi-process retention CHECKS PASSED")
