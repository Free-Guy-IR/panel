import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta

ROOT = os.environ.get("PANEL_ROOT", "/root/dev/panel")
sys.path.insert(0, ROOT)

failures = []


def check(label, got, want):
    ok = got == want
    print("  %-70s %s" % (label, "OK" if ok else "FAIL got=%r want=%r" % (got, want)))
    if not ok:
        failures.append(label)


def main():
    from app.fork.traffic_log.collector import Bucket, NodeState, TrafficCollector, bucket_start_of

    collector = TrafficCollector()
    watermark = datetime.now(UTC).replace(microsecond=0)
    start = bucket_start_of(watermark)

    def key(host):
        return (7, None, 9001, "in", host, 443, "tcp", "DIRECT", False, start)

    collector._buckets = {
        key("older.example"): Bucket(
            first_seen=watermark - timedelta(minutes=2),
            last_seen=watermark - timedelta(minutes=1),
            hits=5,
            route="DIRECT",
            flushed_hits=5,
        ),
        key("straddling.example"): Bucket(
            first_seen=watermark - timedelta(minutes=2),
            last_seen=watermark + timedelta(seconds=30),
            hits=9,
            route="DIRECT",
            flushed_hits=4,
        ),
        key("newer.example"): Bucket(
            first_seen=watermark + timedelta(seconds=5),
            last_seen=watermark + timedelta(seconds=40),
            hits=3,
            route="DIRECT",
        ),
        key("fully.flushed.example"): Bucket(
            first_seen=watermark + timedelta(seconds=5),
            last_seen=watermark + timedelta(seconds=6),
            hits=2,
            route="DIRECT",
            flushed_hits=2,
        ),
    }
    collector._max_row_id = 4242
    collector._ceiling_floor = 99

    print("=== a full purge must not discard events that arrived while it ran ===")
    collector.forget_buckets(None, keep_after=watermark)
    hosts = {k[4] for k in collector._buckets}
    print("    buckets kept: %s" % (sorted(hosts) or "<none>"))

    check("a bucket entirely older than the purge is dropped", "older.example" in hosts, False)
    check("a bucket that straddles the purge is KEPT", "straddling.example" in hosts, True)
    check("a bucket created after the purge is kept", "newer.example" in hosts, True)
    check("a bucket flushed AFTER the purge began is kept, because the purge deleted its row", "fully.flushed.example" in hosts, True)

    straddling = collector._buckets.get(key("straddling.example"))
    check("the straddling bucket keeps every hit, because none of them are in the database now", straddling.hits if straddling else None, 9)
    written_after = collector._buckets.get(key("fully.flushed.example"))
    check("and so does one whose every hit had been written", written_after.hits if written_after else None, 2)
    check("its first_seen is moved up to the purge moment", straddling.first_seen if straddling else None, watermark)
    check("its last_seen is untouched", straddling.last_seen if straddling else None, watermark + timedelta(seconds=30))
    check("every survivor starts unwritten", {b.row_id for b in collector._buckets.values()}, {None})
    check("every survivor has nothing counted as written", {b.flushed_hits for b in collector._buckets.values()}, {0})
    check("the row watermark was reset", collector._max_row_id, 0)
    check("the ceiling floor was reset", collector._ceiling_floor, 0)
    check("the identity watch list was reset", collector._seen_ids, set())

    print()
    print("=== a straddling bucket carries its unwritten pre-purge hits across, by design ===")
    collector._buckets = {
        key("attribution.example"): Bucket(
            first_seen=watermark - timedelta(minutes=2),
            last_seen=watermark + timedelta(seconds=30),
            hits=9,
            route="DIRECT",
        )
    }
    collector.forget_buckets(None, keep_after=watermark)
    carried = collector._buckets.get(key("attribution.example"))
    check("every hit survives the purge", carried.hits if carried else None, 9)
    check("and they are all re-dated to the purge moment", carried.first_seen if carried else None, watermark)

    print()
    print("=== a slow viewer's dropped lines are counted, not silently discarded ===")
    state = NodeState(node_id=9001, name="purge-race-test")
    collector._states[9001] = state
    before = state.dropped_viewer
    collector.note_viewer_drop(9001)
    collector.note_viewer_drop(9001)
    check("two dropped lines were counted", state.dropped_viewer - before, 2)
    collector.note_viewer_drop(4040404)
    check("a drop for an unknown node left the known counter alone", state.dropped_viewer - before, 2)
    check("a drop for an unknown node created no state for it", 4040404 in collector._states, False)


if __name__ == "__main__":
    main()
    print()
    if failures:
        print("FAILED %d check(s): %s" % (len(failures), failures))
        sys.exit(1)
    print("ALL purge-race CHECKS PASSED")
    sys.exit(0)
