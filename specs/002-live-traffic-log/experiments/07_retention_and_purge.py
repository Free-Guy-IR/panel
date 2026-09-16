import json
import subprocess
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

PANEL_ROOT = os.environ.get("PANEL_ROOT", "/root/dev/panel")

BASE = "http://127.0.0.1:8001"
TOKEN = open(os.environ.get("TOKEN_FILE", "/root/dev/.filter_token")).read().strip()
failures = []


def check(label, got, want):
    ok = got == want
    print("  %-64s %s" % (label, "OK" if ok else "FAIL got=%r want=%r" % (got, want)))
    if not ok:
        failures.append(label)


def api(path, method="GET", payload=None, token=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method)
    request.add_header("Authorization", "Bearer " + (token or TOKEN))
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        body = error.read().decode()
        try:
            return error.code, json.loads(body)
        except ValueError:
            return error.code, body[:300]


def iso(moment):
    return moment.isoformat().replace("+00:00", "Z")


def seed(rows, hours_old):
    import sqlite3

    db = sqlite3.connect(os.path.join(PANEL_ROOT, "db.sqlite3"), timeout=60)
    when = datetime.now(UTC) - timedelta(hours=hours_old)
    stamp = when.strftime("%Y-%m-%d %H:%M:%S.%f")
    ids = []
    for index in range(rows):
        cur = db.execute(
            "insert into traffic_log_records (bucket_start,user_id,user_label,node_id,inbound_tag,host,port,protocol,refused,route,first_seen,last_seen,hits)"
            " values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (stamp, 72, None, 5, "Retention Probe", "age%dh-%d.test" % (hours_old, index), 443, "tcp", 0, "DIRECT", stamp, stamp, 1),
        )
        ids.append(cur.lastrowid)
    db.commit()
    db.close()
    return ids


def count_ids(ids):
    import sqlite3

    db = sqlite3.connect("file:%s?mode=ro" % os.path.join(PANEL_ROOT, "db.sqlite3"), uri=True)
    marks = ",".join("?" * len(ids))
    n = db.execute("select count(*) from traffic_log_records where id in (%s)" % marks, ids).fetchone()[0]
    db.close()
    return n


def drop_ids(ids):
    import sqlite3

    db = sqlite3.connect(os.path.join(PANEL_ROOT, "db.sqlite3"), timeout=60)
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        db.execute("delete from traffic_log_records where id in (%s)" % ",".join("?" * len(chunk)), chunk)
    db.commit()
    db.close()


print("=== the migration gave the pre-existing state row a real default ===")
import sqlite3

db = sqlite3.connect("file:%s?mode=ro" % os.path.join(PANEL_ROOT, "db.sqlite3"), uri=True)
cols = [r[1] for r in db.execute("pragma table_info(traffic_log_state)")]
check("traffic_log_state has retention_hours", "retention_hours" in cols, True)
row = db.execute("select id, enabled, retention_hours from traffic_log_state").fetchall()
db.close()
print("    state rows:", row)
check("exactly one state row", len(row), 1)
check("its retention is a usable number", isinstance(row[0][2], int) and row[0][2] >= 1, True)

print("\n=== status reports the live retention ===")
status_code, status = api("/api/traffic-log/status")
check("GET /status -> 200", status_code, 200)
original = status["retention_hours"]
print("    retention now:", original)

print("\n=== retention is settable and takes effect immediately ===")
code, body = api("/api/traffic-log/settings", "PUT", {"retention_hours": 6})
check("PUT retention_hours=6 -> 200", code, 200)
check("status echoes the new retention", body.get("retention_hours"), 6)
code, again = api("/api/traffic-log/status")
check("a fresh status still reports 6", again.get("retention_hours"), 6)

print("\n=== the history window follows the configured retention, at the boundary ===")
now = datetime.now(UTC)
code, _ = api("/api/traffic-log/history?start=%s&end=%s" % (iso(now - timedelta(hours=5, minutes=30)), iso(now)))
check("a start 5h30m back is accepted while retention is 6h", code, 200)
code, detail = api("/api/traffic-log/history?start=%s&end=%s" % (iso(now - timedelta(hours=7)), iso(now)))
check("a start 7h back is refused while retention is 6h", code, 422)
message = detail.get("detail") if isinstance(detail, dict) else str(detail)
check("the refusal names the configured window", "6" in str(message), True)
print("    refusal text:", str(message)[:110])

print("\n=== bounds are enforced ===")
code, _ = api("/api/traffic-log/settings", "PUT", {"retention_hours": 0})
check("retention 0 is rejected", code, 422)
code, _ = api("/api/traffic-log/settings", "PUT", {"retention_hours": 721})
check("retention 721 is rejected", code, 422)
code, _ = api("/api/traffic-log/settings", "PUT", {})
check("an empty settings body is rejected", code, 422)

print("\n=== the automatic purge uses the configured window ===")
api("/api/traffic-log/settings", "PUT", {"retention_hours": 6})
old_ids = seed(40, 9)
young_ids = seed(40, 2)
check("seeded rows are present", count_ids(old_ids + young_ids), 80)
sys.path.insert(0, PANEL_ROOT)
import asyncio

from app.fork.jobs.traffic_log_purge import purge_traffic_log


asyncio.run(purge_traffic_log())
check("rows older than the 6h window were purged", count_ids(old_ids), 0)
check("rows inside the window survived", count_ids(young_ids), 40)

print("\n=== on-demand purge: older-than ===")
old_ids = seed(30, 9)
young_ids2 = seed(30, 1)
code, result = api("/api/traffic-log/purge", "POST", {"older_than_hours": 6})
check("POST /purge older_than_hours=6 -> 200", code, 200)
check("it removed the aged rows", count_ids(old_ids), 0)
check("it kept the fresh rows", count_ids(young_ids2), 30)
check("the result reports a positive removal count", isinstance(result.get("removed"), int) and result["removed"] >= 30, True)
print("    purge result:", json.dumps(result))

print("\n=== on-demand purge: everything ===")
before_total = api("/api/traffic-log/purge", "POST", {"older_than_hours": 999999})[1]
code, result = api("/api/traffic-log/purge", "POST", {})
check("POST /purge with no body -> 200", code, 200)
check("nothing remains afterwards", result.get("remaining"), 0)
db = sqlite3.connect("file:%s?mode=ro" % os.path.join(PANEL_ROOT, "db.sqlite3"), uri=True)
actual = db.execute("select count(*) from traffic_log_records").fetchone()[0]
db.close()
check("the database agrees the table is empty", actual, 0)

print("\n=== a flush cannot resurrect what was purged ===")
time.sleep(8)
db = sqlite3.connect("file:%s?mode=ro" % os.path.join(PANEL_ROOT, "db.sqlite3"), uri=True)
after_flush = db.execute("select count(*) from traffic_log_records where host like ?", ("%.test",)).fetchone()[0]
db.close()
check("no purged synthetic row came back", after_flush, 0)

drop_ids(young_ids + young_ids2)
api("/api/traffic-log/settings", "PUT", {"retention_hours": original})
code, restored = api("/api/traffic-log/status")
check("retention restored to its original value", restored.get("retention_hours"), original)

print()
if failures:
    print("FAILED %d check(s): %s" % (len(failures), failures))
    raise SystemExit(1)
print("ALL retention/purge CHECKS PASSED")
