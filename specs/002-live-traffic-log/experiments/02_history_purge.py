#!/usr/bin/env python3
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta

ROOT = os.environ.get("PANEL_ROOT", "/root/dev/panel")
BASE = os.environ.get("PANEL_URL", "http://127.0.0.1:8001")
TOKEN = open(os.environ.get("TOKEN_FILE", "/root/dev/.filter_token")).read().strip()
H = {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"}
KIDS_PORT = 10821
OPEN_PORT = 10822
CEILING = 50
OLD_ROWS = 120
RECENT_ROWS = 80
DEFAULT_CEILING = 2_000_000
LOAD_BATCH = 5_000
MAX_PURGE_INTERVAL = 900
COPY_NAME = "traffic_log_ceiling_copy.sqlite3"
NODE_STATES = {"attaching", "collecting", "error", "detached", "paused", "unavailable", "no_reports"}
NODE_FIELDS = ("node_id", "node", "state", "since", "lines", "events", "dropped", "records", "last_event_at", "detail")
PURGE_DEADLINE = int(os.environ.get("TL_PURGE_DEADLINE", "700"))
HISTORY_VISIBLE_DEADLINE = int(os.environ.get("TL_HISTORY_DEADLINE", "60"))
CLOCK_SLACK = timedelta(seconds=3)
CEILING_HTTP_DEADLINE = int(os.environ.get("TL_CEILING_HTTP_DEADLINE", "700"))
SKIP_LOAD = "--skip-load" in sys.argv
failures = []


def arg_value(name, default=None):
    prefix = "--%s=" % name
    for item in sys.argv[1:]:
        if item.startswith(prefix):
            return item[len(prefix) :]
    return default


PHASE = arg_value("phase")
LOAD_ROWS = int(arg_value("load-rows", os.environ.get("TL_LOAD_ROWS", str(DEFAULT_CEILING))))


def check(label, got, want):
    ok = got == want
    print("  %-62s %s" % (label, "OK" if ok else "FAIL got=%r want=%r" % (got, want)))
    if not ok:
        failures.append(label)


def call(method, path, body=None, headers=H, timeout=60):
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


def q(path, **params):
    return path + "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})


def iso(t):
    return t.isoformat()


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


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


def detail_text(body):
    if isinstance(body, dict):
        return json.dumps(body.get("detail", body))
    return str(body)


def status():
    st, body = call("GET", "/api/traffic-log/status")
    return st, (body if isinstance(body, dict) else {})


def without_nodes(payload):
    return json.dumps({k: v for k, v in payload.items() if k != "nodes"}, default=str)


def part1_history_over_http():
    print("=== part 1: history and summary over HTTP ===")
    marker = datetime.now(UTC) - CLOCK_SLACK
    wait_all(
        [
            curl_via(KIDS_PORT, "www.wikipedia.org"),
            curl_via(KIDS_PORT, "www.google.com"),
            curl_via(KIDS_PORT, "www.pornhub.com"),
            curl_via(OPEN_PORT, "www.wikipedia.org"),
        ]
    )

    st = None
    items = []
    now = datetime.now(UTC)
    deadline = time.monotonic() + HISTORY_VISIBLE_DEADLINE
    while time.monotonic() < deadline:
        time.sleep(2)
        now = datetime.now(UTC)
        st, body = call("GET", q("/api/traffic-log/history", start=iso(now - timedelta(hours=1)), end=iso(now), username="demo-kids", limit=200))
        items = body.get("items", []) if isinstance(body, dict) else []
        if st == 200 and any(i.get("host") == "www.pornhub.com" and parse_ts(i["last_seen"]) >= marker for i in items):
            break
    start = now - timedelta(hours=1)
    check("history last hour for demo-kids -> 200", st, 200)
    print("    %d items: %s" % (len(items), [(i.get("host"), i.get("hits"), i.get("refused")) for i in items][:10]))
    check("history contains at least one item", len(items) >= 1, True)
    porn = [i for i in items if i.get("host") == "www.pornhub.com"]
    check("history lists www.pornhub.com", len(porn) >= 1, True)
    mine = [i for i in porn if parse_ts(i["last_seen"]) >= marker]
    check("this run's own www.pornhub.com rows are refused", bool(mine) and all(i.get("refused") is True for i in mine), True)
    check("every item has hits >= 1", all(isinstance(i.get("hits"), int) and i["hits"] >= 1 for i in items), True)
    check("every item has first_seen <= last_seen", all(parse_ts(i["first_seen"]) <= parse_ts(i["last_seen"]) for i in items), True)
    check("every item belongs to demo-kids", all(i.get("username") == "demo-kids" and i.get("user_id") == 72 for i in items), True)
    check("every item is inside the requested range", all(start <= parse_ts(i["last_seen"]) <= now + timedelta(seconds=5) for i in items), True)
    keys = [(parse_ts(i["last_seen"]), i["id"]) for i in items]
    check("items ordered by last_seen desc, id desc", keys, sorted(keys, reverse=True))
    check("every item names node filter-test-xray", all(i.get("node") == "filter-test-xray" for i in items if i.get("node_id") == 5), True)
    check("items carry user_deleted false", all(i.get("user_deleted") is False for i in items), True)

    st, body = call("GET", q("/api/traffic-log/history", start=iso(now - timedelta(days=3)), end=iso(now), username="demo-kids"))
    check("start=now-3d -> 422", st, 422)
    check("422 detail mentions the 48 hours limit", "48 hours" in detail_text(body), True)
    st, body = call("GET", q("/api/traffic-log/history", start=iso(now), end=iso(now - timedelta(hours=1))))
    check("end <= start -> 422", st, 422)

    st, page1 = call("GET", q("/api/traffic-log/history", start=iso(start), end=iso(now), username="demo-kids", limit=2))
    check("page 1 (limit=2) -> 200", st, 200)
    items1 = page1.get("items", []) if isinstance(page1, dict) else []
    cursor = page1.get("next_cursor") if isinstance(page1, dict) else None
    check("page 1 has 2 items", len(items1), 2)
    check("page 1 carries next_cursor", bool(cursor), True)
    st, page2 = call("GET", q("/api/traffic-log/history", start=iso(start), end=iso(now), username="demo-kids", limit=2, cursor=cursor))
    check("page 2 via next_cursor -> 200", st, 200)
    items2 = page2.get("items", []) if isinstance(page2, dict) else []
    check("page 2 has at least one item", len(items2) >= 1, True)
    ids1 = {i["id"] for i in items1}
    ids2 = {i["id"] for i in items2}
    check("no duplicate ids across pages", ids1 & ids2, set())
    keys1 = [(parse_ts(i["last_seen"]), i["id"]) for i in items1]
    keys2 = [(parse_ts(i["last_seen"]), i["id"]) for i in items2]
    check("page 2 continues strictly after page 1", bool(keys1) and bool(keys2) and max(keys2) < min(keys1), True)
    check("pages together equal the unpaged prefix", [i["id"] for i in items1 + items2], [i["id"] for i in items[: len(items1) + len(items2)]])

    st, summary = call("GET", q("/api/traffic-log/summary", start=iso(start), end=iso(now), username="demo-kids"))
    check("summary for demo-kids -> 200", st, 200)
    summary = summary if isinstance(summary, dict) else {}
    print("    summary: %s" % json.dumps(summary)[:400])
    check("summary refused >= 1", isinstance(summary.get("refused"), int) and summary["refused"] >= 1, True)
    check("summary connections >= refused", isinstance(summary.get("connections"), int) and summary["connections"] >= summary.get("refused", 0), True)
    check("summary users == 1", summary.get("users"), 1)
    check("summary top_destinations contains www.pornhub.com", any(d.get("host") == "www.pornhub.com" for d in summary.get("top_destinations", [])), True)
    check("summary top_users is demo-kids only", [u.get("username") for u in summary.get("top_users", [])], ["demo-kids"])
    check("summary top lists are capped at 8", len(summary.get("top_users", [])) <= 8 and len(summary.get("top_destinations", [])) <= 8, True)


def part2_status_over_http():
    print("\n=== part 2: the operator's status surface over HTTP (FR-010/011/012) ===")
    st, body = status()
    check("GET /status -> 200", st, 200)
    print("    %s" % without_nodes(body))
    check("status: available true", body.get("available"), True)
    check("status: enabled true", body.get("enabled"), True)
    check("status: retention_hours is 48", body.get("retention_hours"), 48)
    check("status: max_records is the configured ceiling", body.get("max_records"), DEFAULT_CEILING)
    check("status: ceiling_active is a boolean", isinstance(body.get("ceiling_active"), bool), True)
    check("status: purged_expired is an integer", isinstance(body.get("purged_expired"), int), True)
    check("status: purged_over_ceiling is an integer", isinstance(body.get("purged_over_ceiling"), int), True)

    nodes = body.get("nodes") or []
    print("    nodes: %s" % [(n.get("node_id"), n.get("state"), n.get("lines"), n.get("events"), n.get("dropped")) for n in nodes])
    check("status: at least one node is listed", len(nodes) >= 1, True)
    check("status: every node state is a documented state", sorted({str(n.get("state")) for n in nodes} - NODE_STATES), [])
    check("status: every node carries the contract fields", sorted({f for n in nodes for f in NODE_FIELDS if f not in n}), [])
    check("status: every node reports a dropped counter", all(isinstance(n.get("dropped"), int) for n in nodes), True)

    started = time.monotonic()
    last_purge = body.get("last_purge_at")
    while last_purge is None and time.monotonic() - started < PURGE_DEADLINE:
        print("    waiting for the scheduled purge to report itself (%.0f s so far)" % (time.monotonic() - started))
        time.sleep(20)
        _, body = status()
        last_purge = body.get("last_purge_at")
    print("    last_purge_at: %r after %.0f s" % (last_purge, time.monotonic() - started))
    check("status: the scheduled purge reported last_purge_at within %d s" % PURGE_DEADLINE, last_purge is not None, True)


def record(host, at, user_id=None, user_label=None):
    bucket = at.replace(second=0, microsecond=0)
    bucket = bucket - timedelta(minutes=bucket.minute % 5)
    return {
        "bucket_start": bucket,
        "user_id": user_id,
        "user_label": user_label,
        "node_id": 5,
        "inbound_tag": "Shadowsocks TCP",
        "host": host,
        "port": 443,
        "protocol": "tcp",
        "refused": False,
        "route": "DIRECT",
        "first_seen": at,
        "last_seen": at,
        "hits": 1,
    }


def enter_panel_root():
    os.chdir(ROOT)
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)


def interval_seconds(job):
    trigger = getattr(job, "trigger", None)
    interval = getattr(trigger, "interval", None)
    if interval is not None:
        return interval.total_seconds()
    length = getattr(trigger, "interval_length", None)
    return float(length) if length is not None else None


def payload_fields(payload):
    if hasattr(payload, "model_dump"):
        return payload.model_dump()
    if isinstance(payload, dict):
        return payload
    return dict(vars(payload))


async def ceiling_phase():
    import sqlalchemy as sa

    from app import scheduler
    from app.db import GetDB
    from app.db.base import engine
    from app.fork.jobs.traffic_log_purge import purge_traffic_log
    from app.fork.models.traffic_log import TrafficLogRecord
    from app.fork.traffic_log import service
    from config import database_settings, job_settings

    table = TrafficLogRecord.__table__

    print("=== part 3: retention + ceiling, in-process, TRAFFIC_LOG_MAX_RECORDS=%d ===" % CEILING)
    print("    !! this part DELETES every record above the ceiling, so it never touches the panel's own database")
    print("    !! it runs against a throwaway copy: %s" % database_settings.url)
    check("the ceiling pass was handed a disposable database", os.environ.get("TL_DISPOSABLE"), "1")
    if os.environ.get("TL_DISPOSABLE") != "1":
        return
    check("job_settings picked up the ceiling from the environment", job_settings.traffic_log_max_records, CEILING)

    async def count(where=None):
        stmt = sa.select(sa.func.count()).select_from(table)
        if where is not None:
            stmt = stmt.where(where)
        async with GetDB() as db:
            return (await db.execute(stmt)).scalar_one()

    async def seeded(prefix):
        async with GetDB() as db:
            rows = (await db.execute(sa.select(table.c.id, table.c.host).where(table.c.host.like(prefix + "%")))).all()
        return {r.id: r.host for r in rows}

    now = datetime.now(UTC).replace(microsecond=0)
    old_at = now - timedelta(days=3)
    recent_at = now - timedelta(minutes=2)
    rows = [record("seed-%d.test" % n, old_at if n < OLD_ROWS else recent_at, user_label="seed") for n in range(OLD_ROWS + RECENT_ROWS)]
    async with GetDB() as db:
        await db.execute(sa.insert(table), rows)
        await db.commit()
    before = await seeded("seed-")
    recent_ids = sorted(i for i, h in before.items() if int(h[5:].split(".")[0]) >= OLD_ROWS)
    old_ids = sorted(i for i, h in before.items() if int(h[5:].split(".")[0]) < OLD_ROWS)
    check("seeded %d rows" % (OLD_ROWS + RECENT_ROWS), len(before), OLD_ROWS + RECENT_ROWS)
    total_before = await count()
    print("    rows in the copy before the purge: %d (max seeded id %d)" % (total_before, max(before)))

    cutoff = now - timedelta(hours=48)
    await purge_traffic_log()
    after = await seeded("seed-")
    total_after = await count()
    expired_left = await count(table.c.bucket_start < cutoff)
    survivors = sorted(after)
    check("no row older than 48 h remains", expired_left, 0)
    check("all 3-day-old seeded rows are gone", [i for i in old_ids if i in after], [])
    check("total rows <= ceiling (%d)" % CEILING, total_after <= CEILING, True)
    check("at least one recent seeded row survived", len(survivors) >= 1, True)
    check("surviving seeded rows are the highest seeded ids", survivors, recent_ids[-len(survivors) :] if survivors else [])
    print("    rows after the purge: %d, surviving seeded: %d" % (total_after, len(survivors)))

    payload = payload_fields(await service.status_payload())
    print("    status payload the operator would receive: %s" % without_nodes(payload))
    check("status payload ceiling_active is True", payload.get("ceiling_active"), True)
    check("status payload retention_hours is 48", payload.get("retention_hours"), 48)
    check("status payload max_records is the configured ceiling", payload.get("max_records"), CEILING)
    check("status payload purged_expired >= %d" % OLD_ROWS, (payload.get("purged_expired") or 0) >= OLD_ROWS, True)
    check("status payload purged_over_ceiling >= 1", (payload.get("purged_over_ceiling") or 0) >= 1, True)
    check("status payload last_purge_at is set", payload.get("last_purge_at") is not None, True)

    job_settings.traffic_log_max_records = DEFAULT_CEILING
    check("ceiling reset to the default for the normal run", job_settings.traffic_log_max_records, DEFAULT_CEILING)
    unchanged_before = await count()
    await purge_traffic_log()
    check("normal run deletes nothing recent", await count(), unchanged_before)
    payload = payload_fields(await service.status_payload())
    print("    status payload after the normal run: ceiling_active=%r" % payload.get("ceiling_active"))

    job = scheduler.get_job("traffic_log_purge")
    check("the purge job is registered as traffic_log_purge", job is not None, True)
    if job is not None:
        seconds = interval_seconds(job)
        print("    traffic_log_purge interval: %r s (TRAFFIC_LOG_PURGE_INTERVAL=%r)" % (seconds, job_settings.traffic_log_purge_interval))
        check(
            "SC-004: the purge job runs at least every %d s" % MAX_PURGE_INTERVAL,
            seconds is not None and seconds <= MAX_PURGE_INTERVAL,
            True,
        )
    check(
        "SC-004: the configured purge interval cannot exceed %d s" % MAX_PURGE_INTERVAL,
        job_settings.traffic_log_purge_interval <= MAX_PURGE_INTERVAL,
        True,
    )

    await engine.dispose()


async def load_phase():
    import sqlalchemy as sa

    from app.db import GetDB
    from app.db.base import engine
    from app.fork.models.traffic_log import TrafficLogRecord
    from config import job_settings

    table = TrafficLogRecord.__table__
    ceiling = job_settings.traffic_log_max_records

    async def count(where=None):
        stmt = sa.select(sa.func.count()).select_from(table)
        if where is not None:
            stmt = stmt.where(where)
        async with GetDB() as db:
            return (await db.execute(stmt)).scalar_one()

    async def delete_prefix(prefix):
        removed = 0
        async with GetDB() as db:
            while True:
                ids = (await db.execute(sa.select(table.c.id).where(table.c.host.like(prefix + "%")).limit(LOAD_BATCH))).scalars().all()
                if not ids:
                    break
                await db.execute(sa.delete(table).where(table.c.id.in_(ids)))
                await db.commit()
                removed += len(ids)
        return removed

    print("=== part 4: history latency with the store at its ceiling (SC-003) ===")
    print("    !! this part seeds %d rows into the panel's OWN database to reach the %d record ceiling" % (LOAD_ROWS, ceiling))
    print("    !! while they are in place max(id) - ceiling turns positive, so the panel's purge starts")
    print("    !! evicting the oldest records — exactly the FR-010 behaviour this part proves")
    print("    !! the seeded rows are deleted again at the end; run this part LAST")

    pre_existing = await count()
    started = time.perf_counter()
    base = datetime.now(UTC).replace(microsecond=0)
    async with GetDB() as db:
        for offset in range(0, LOAD_ROWS, LOAD_BATCH):
            batch = [
                record("load-%d.test" % n, base - timedelta(seconds=n % 3600), user_id=72 if n % 2 == 0 else 73)
                for n in range(offset, min(offset + LOAD_BATCH, LOAD_ROWS))
            ]
            await db.execute(sa.insert(table), batch)
            await db.commit()
    print("    seeded %d rows in %.1f s (%d rows were already there)" % (LOAD_ROWS, time.perf_counter() - started, pre_existing))
    check("load rows present", await count(table.c.host.like("load-%")), LOAD_ROWS)

    total_at_timing = await count()
    print("    SC-003 measured with %d rows in the store against a ceiling of %d" % (total_at_timing, ceiling))
    check("SC-003 was measured with the store at or above its ceiling", total_at_timing >= ceiling, True)

    now2 = datetime.now(UTC)
    t = time.perf_counter()
    st, body = call("GET", q("/api/traffic-log/history", start=iso(now2 - timedelta(hours=2)), end=iso(now2), limit=100))
    elapsed = time.perf_counter() - t
    items = body.get("items", []) if isinstance(body, dict) else []
    check("history first page -> 200", st, 200)
    check("history first page under 2 s (%.2f s)" % elapsed, elapsed < 2.0, True)
    check("history first page holds 100 items", len(items), 100)
    t = time.perf_counter()
    st, body = call("GET", q("/api/traffic-log/history", start=iso(now2 - timedelta(hours=2)), end=iso(now2), username="demo-kids", limit=100))
    elapsed = time.perf_counter() - t
    check("history first page for demo-kids under 2 s (%.2f s)" % elapsed, st == 200 and elapsed < 2.0, True)
    t = time.perf_counter()
    st, body = call("GET", q("/api/traffic-log/summary", start=iso(now2 - timedelta(hours=2)), end=iso(now2)), timeout=120)
    print("    summary over the load took %.2f s (HTTP %s)" % (time.perf_counter() - t, st))

    if CEILING_HTTP_DEADLINE <= 0:
        print("    skipping the ceiling_active check over HTTP (TL_CEILING_HTTP_DEADLINE=0)")
    else:
        print("    waiting for the panel's own purge to report the ceiling over HTTP (up to %d s)" % CEILING_HTTP_DEADLINE)
        waited = time.monotonic()
        body = {}
        while time.monotonic() - waited < CEILING_HTTP_DEADLINE:
            _, body = status()
            if body.get("ceiling_active") is True:
                break
            time.sleep(20)
        print("    status after %.0f s: %s" % (time.monotonic() - waited, without_nodes(body)))
        check("FR-010: GET /status reports ceiling_active true at the ceiling", body.get("ceiling_active"), True)
        check("FR-010: GET /status reports purged_over_ceiling >= 1", (body.get("purged_over_ceiling") or 0) >= 1, True)
        check("FR-010: GET /status still reports max_records", body.get("max_records"), ceiling)
        check("FR-010: GET /status carries last_purge_at", body.get("last_purge_at") is not None, True)

    started = time.perf_counter()
    removed = await delete_prefix("load-")
    print("    deleted %d load rows in %.1f s" % (removed, time.perf_counter() - started))
    check("no load rows left behind", await count(table.c.host.like("load-%")), 0)

    await engine.dispose()


def database_url():
    enter_panel_root()
    from config import database_settings

    return database_settings.url


def disposable_database():
    override = os.environ.get("TL_DISPOSABLE_DB_URL")
    if override:
        return override, None
    url = database_url()
    if not url.startswith("sqlite"):
        return None, None
    _, _, rest = url.partition(":///")
    source = rest if os.path.isabs(rest) else os.path.join(ROOT, rest)
    target = os.path.join(tempfile.gettempdir(), COPY_NAME)
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(target + suffix):
            os.remove(target + suffix)
    live = sqlite3.connect("file:%s?mode=ro" % source, uri=True)
    copy = sqlite3.connect(target)
    with copy:
        live.backup(copy)
    live.close()
    copy.close()
    print("    copied %s -> %s (%.1f MB)" % (source, target, os.path.getsize(target) / 1e6))
    return "sqlite+aiosqlite:///%s" % target, target


def run_phase(name, extra_env):
    env = dict(os.environ)
    env.update(extra_env)
    argv = [sys.executable, os.path.abspath(__file__), "--phase=" + name]
    if name == "load":
        argv.append("--load-rows=%d" % LOAD_ROWS)
    print()
    completed = subprocess.run(argv, env=env, cwd=ROOT)
    return completed.returncode


def finish(label):
    print()
    if failures:
        print("FAILED %d check(s): %s" % (len(failures), failures))
        sys.exit(1)
    print("ALL %s CHECKS PASSED" % label)
    sys.exit(0)


def main():
    import asyncio

    if PHASE == "ceiling":
        enter_panel_root()
        asyncio.run(ceiling_phase())
        finish("ceiling")
    if PHASE == "load":
        enter_panel_root()
        asyncio.run(load_phase())
        finish("load")

    part1_history_over_http()
    part2_status_over_http()

    print("\n=== preparing a disposable database for the ceiling pass ===")
    url, copy_path = disposable_database()
    check("a disposable database is available for the ceiling pass", url is not None, True)
    if url is not None:
        rc = run_phase("ceiling", {"SQLALCHEMY_DATABASE_URL": url, "TRAFFIC_LOG_MAX_RECORDS": str(CEILING), "TL_DISPOSABLE": "1"})
        check("the ceiling pass finished without failures", rc, 0)
        if copy_path:
            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(copy_path + suffix):
                    os.remove(copy_path + suffix)
            print("    removed the disposable copy")
    else:
        print("    the panel database is not SQLite; set TL_DISPOSABLE_DB_URL to a throwaway copy and re-run")

    if SKIP_LOAD:
        print("\n=== part 4: skipped (--skip-load) ===")
    else:
        rc = run_phase("load", {})
        check("the load pass finished without failures", rc, 0)

    finish("history-purge")


main()
