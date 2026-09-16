import asyncio
import os
import shutil
import sys
import tempfile
from datetime import UTC, datetime, timedelta

ROOT = os.environ.get("PANEL_ROOT", "/root/dev/panel")
SOURCE = os.path.join(ROOT, "db.sqlite3")

failures = []


def check(label, got, want):
    ok = got == want
    print("  %-62s %s" % (label, "OK" if ok else "FAIL got=%r want=%r" % (got, want)))
    if not ok:
        failures.append(label)


def finish():
    print()
    if failures:
        print("FAILED %d check(s): %s" % (len(failures), failures))
        sys.exit(1)
    print("ALL bucket-key CHECKS PASSED")
    sys.exit(0)


def prepare():
    target = os.path.join(tempfile.mkdtemp(prefix="tl_bucket_key_"), "copy.sqlite3")
    shutil.copy2(SOURCE, target)
    os.environ["SQLALCHEMY_DATABASE_URL"] = "sqlite+aiosqlite:///%s" % target
    os.environ["TRAFFIC_LOG_MAX_RECORDS"] = "0"
    sys.path.insert(0, ROOT)
    return target


async def main(target):
    from sqlalchemy import select

    from app.db import GetDB
    from app.fork.models.traffic_log import TrafficLogRecord
    from app.fork.traffic_log.collector import NodeState, TrafficCollector, bucket_start_of
    from app.fork.traffic_log.parse import Event

    print("=== a disposable copy of the panel database is used ===")
    check("the copy exists", os.path.exists(target), True)

    collector = TrafficCollector()
    state = NodeState(node_id=9001, name="bucket-key-test")

    at = datetime.now(UTC).replace(microsecond=0)
    common = dict(
        at=at,
        user_id=None,
        user_label="bucket-key-probe",
        node_id=9001,
        inbound="probe-inbound",
        host="key.example",
        port=443,
        protocol="tcp",
    )

    print()
    print("=== two events that differ ONLY in the outbound must not merge ===")
    collector._fold(Event(route="DIRECT", refused=False, **common), state)
    collector._fold(Event(route="SECOND", refused=False, **common), state)
    collector._fold(Event(route="DIRECT", refused=False, **common), state)

    check("the two outbounds produced two buckets", len(collector._buckets), 2)
    widths = {len(key) for key in collector._buckets}
    check("every key carries all ten fields", widths, {10})
    hits = sorted(bucket.hits for bucket in collector._buckets.values())
    check("the repeated outbound accumulated its hits", hits, [1, 2])

    print()
    print("=== the flush must write them, which is what the nine-field unpack broke ===")
    await collector._flush()

    async with GetDB() as db:
        rows = (
            await db.execute(
                select(TrafficLogRecord.route, TrafficLogRecord.hits, TrafficLogRecord.host).where(
                    TrafficLogRecord.node_id == 9001
                )
            )
        ).all()
    written = sorted((row[0], row[1]) for row in rows)
    print("    rows written: %r" % (written,))
    check("both rows reached the database", len(rows), 2)
    check("each row kept its own outbound and hit count", written, [("DIRECT", 2), ("SECOND", 1)])
    check("every row carries the destination", {row[2] for row in rows}, {"key.example"})

    print()
    print("=== expiring an old bucket must compare the bucket start, not the refused flag ===")
    stale = dict(common)
    stale["at"] = at - timedelta(minutes=30)
    collector._fold(Event(route="DIRECT", refused=False, **stale), state)
    before = len(collector._buckets)
    stale_start = bucket_start_of(stale["at"])
    check("the stale event opened its own bucket", any(key[-1] == stale_start for key in collector._buckets), True)

    await collector._flush()
    after = len(collector._buckets)
    print("    buckets before the flush %d, after %d" % (before, after))
    check("the stale bucket was expired by the flush", before - after, 1)
    check("no stale bucket survived", any(key[-1] == stale_start for key in collector._buckets), False)
    check("the current buckets survived", {key[-1] for key in collector._buckets}, {bucket_start_of(at)})

    async with GetDB() as db:
        from sqlalchemy import delete

        await db.execute(delete(TrafficLogRecord).where(TrafficLogRecord.node_id == 9001))
        await db.commit()


if __name__ == "__main__":
    copy = prepare()
    try:
        asyncio.run(main(copy))
    finally:
        shutil.rmtree(os.path.dirname(copy), ignore_errors=True)
    finish()
