import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
PANEL_ROOT = os.environ.get("PANEL_ROOT", "/root/dev/panel")

BASE = "http://127.0.0.1:8001"
TOKEN = open(os.environ.get("TOKEN_FILE", "/root/dev/.filter_token")).read().strip()
DB = os.path.join(PANEL_ROOT, "db.sqlite3")
failures = []
stop = threading.Event()
reader_errors = []
writer_errors = []


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
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.loads(response.read() or b"null")


def sizes():
    total = 0
    for suffix in ("", "-wal"):
        path = DB + suffix
        if os.path.exists(path):
            total += os.path.getsize(path)
    return total


def seed(rows):
    import sqlite3
    from datetime import UTC, datetime

    db = sqlite3.connect(DB, timeout=120)
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
    batch = []
    for index in range(rows):
        batch.append((stamp, 72, None, 5, "Reclaim Probe", "reclaim-%d.test" % index, 443, "tcp", 0, "DIRECT", stamp, stamp, 1))
        if len(batch) == 5000:
            db.executemany(
                "insert into traffic_log_records (bucket_start,user_id,user_label,node_id,inbound_tag,host,port,protocol,refused,route,first_seen,last_seen,hits)"
                " values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                batch,
            )
            db.commit()
            batch = []
    if batch:
        db.executemany(
            "insert into traffic_log_records (bucket_start,user_id,user_label,node_id,inbound_tag,host,port,protocol,refused,route,first_seen,last_seen,hits)"
            " values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            batch,
        )
        db.commit()
    db.close()


def keep_reading():
    while not stop.is_set():
        try:
            api("/api/traffic-log/status")
        except Exception as error:
            reader_errors.append(repr(error)[:120])
        time.sleep(0.4)


def keep_writing():
    import sqlite3
    from datetime import UTC, datetime

    while not stop.is_set():
        try:
            db = sqlite3.connect(DB, timeout=30)
            stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
            db.execute(
                "insert into traffic_log_records (bucket_start,user_id,user_label,node_id,inbound_tag,host,port,protocol,refused,route,first_seen,last_seen,hits)"
                " values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (stamp, 73, None, 5, "Concurrent Writer", "writer.test", 443, "tcp", 0, "DIRECT", stamp, stamp, 1),
            )
            db.commit()
            db.close()
        except Exception as error:
            writer_errors.append(repr(error)[:120])
        time.sleep(0.5)


print("=== seeding 250000 rows so there is real space to reclaim ===")
seed(250000)
grown = sizes()
print("    database plus wal: %.1f MB" % (grown / 1048576))
check("the database really grew", grown > 20 * 1048576, True)

print("\n=== one purge call is capped, and says so honestly ===")
first = api("/api/traffic-log/purge", "POST", {"reclaim": False})
print("    first call: %s" % json.dumps({k: first[k] for k in ("removed", "remaining", "incomplete", "reclaimed", "freed_bytes")}))
check("the first call removed a large batch", first["removed"] > 0, True)
check("it reports incomplete while rows remain", first["incomplete"], first["remaining"] > 0)
check("it honestly reports no reclamation", first["reclaimed"], False)

print("\n=== repeating until done, exactly as the dashboard button does ===")
rounds = 1
result = first
while result["incomplete"] and result["removed"] > 0 and rounds < 10:
    result = api("/api/traffic-log/purge", "POST", {"reclaim": False})
    rounds += 1
print("    finished after %d call(s): %s" % (rounds, json.dumps({k: result[k] for k in ("removed", "remaining", "incomplete")})))
check("every record is gone", result["remaining"], 0)
check("the last call is not marked incomplete", result["incomplete"], False)
after_plain = sizes()
check("but the files did NOT shrink", after_plain > 20 * 1048576, True)
print("    still on disk: %.1f MB" % (after_plain / 1048576))
seed(60000)
print("    re-seeded 60000 rows so the reclaim stage has something to free")

print("\n=== now reclaim, with readers and a writer hammering the database ===")
readers = [threading.Thread(target=keep_reading, daemon=True) for _ in range(3)]
writer = threading.Thread(target=keep_writing, daemon=True)
for thread in readers + [writer]:
    thread.start()
time.sleep(1.5)
started = time.time()
result = api("/api/traffic-log/purge", "POST", {"reclaim": True})
elapsed = time.time() - started
stop.set()
time.sleep(1.5)
after_reclaim = sizes()
print("    result: %s" % json.dumps({k: result[k] for k in ("removed", "remaining", "reclaimed", "freed_bytes")}))
print("    took %.1fs, files now %.1f MB" % (elapsed, after_reclaim / 1048576))
check("the endpoint answered", isinstance(result.get("reclaimed"), bool), True)
if result["reclaimed"]:
    check("it reported freed bytes", isinstance(result.get("freed_bytes"), int), True)
    check("the files really shrank", after_reclaim < after_plain / 2, True)
else:
    print("    reclamation did not finish inside its budget; waiting for the background task")
    for _ in range(30):
        time.sleep(2)
        if sizes() < after_plain / 2:
            break
    check("the background reclamation still shrank the files", sizes() < after_plain / 2, True)

print("\n=== the panel stayed usable throughout ===")
check("no reader saw an error", reader_errors[:3], [])
print("    writer errors (a busy database is acceptable, a corrupt one is not): %d" % len(writer_errors))
for error in writer_errors[:2]:
    print("     ", error)
check("no writer error mentions corruption", any("corrupt" in e.lower() or "malformed" in e.lower() for e in writer_errors), False)
status = api("/api/traffic-log/status")
check("status still answers", isinstance(status.get("nodes"), list), True)

import sqlite3

db = sqlite3.connect(DB, timeout=60)
db.execute("delete from traffic_log_records where host in ('writer.test') or host like 'reclaim-%.test'")
db.commit()
db.close()

print()
if failures:
    print("FAILED %d check(s): %s" % (len(failures), failures))
    raise SystemExit(1)
print("ALL reclamation CHECKS PASSED")
