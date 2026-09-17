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
    collector.reseed_row_watermark(0, 0)
    collector.detach_missing_rows(set())
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
    collector.reseed_row_watermark(0, 0)
    collector.detach_missing_rows(set())
    carried = collector._buckets.get(key("attribution.example"))
    check("every hit survives the purge", carried.hits if carried else None, 9)
    check("and they are all re-dated to the purge moment", carried.first_seen if carried else None, watermark)

    print()
    print("=== an age-based purge must not take events that arrived after its cutoff ===")
    collector._buckets = {
        key("entirely.old.example"): Bucket(
            first_seen=watermark - timedelta(minutes=4),
            last_seen=watermark - timedelta(minutes=1),
            hits=6,
            route="DIRECT",
            flushed_hits=6,
        ),
        key("straddles.cutoff.example"): Bucket(
            first_seen=watermark - timedelta(minutes=4),
            last_seen=watermark + timedelta(seconds=20),
            hits=7,
            route="DIRECT",
            flushed_hits=3,
        ),
    }
    collector._max_row_id = 77
    collector._ceiling_floor = 11
    collector.forget_buckets(watermark)
    collector.reseed_row_watermark(77, 5)
    collector.detach_missing_rows(set())
    aged = {k[4] for k in collector._buckets}
    check("a bucket whose last event predates the cutoff is dropped", "entirely.old.example" in aged, False)
    check("a bucket still receiving events after the cutoff is KEPT", "straddles.cutoff.example" in aged, True)
    kept = collector._buckets.get(key("straddles.cutoff.example"))
    check("it keeps every hit, because the purge deleted its row too", kept.hits if kept else None, 7)
    check("it is re-dated to the cutoff", kept.first_seen if kept else None, watermark)
    check("and it counts as unwritten again", (kept.row_id, kept.flushed_hits) if kept else None, (None, 0))
    check("an age-based purge leaves the row watermark alone", collector._max_row_id, 77)
    check("and leaves the ceiling floor alone", collector._ceiling_floor, 11)

    print()
    print("=== survival is decided by ingestion order, so a clock that steps backwards cannot lose an event ===")
    collector._ingest_seq = 500
    collector._buckets = {
        key("before.the.request.example"): Bucket(
            first_seen=watermark - timedelta(minutes=1),
            last_seen=watermark - timedelta(seconds=30),
            hits=4,
            route="DIRECT",
            last_ingest=500,
        ),
        key("arrived.during.the.purge.example"): Bucket(
            first_seen=watermark - timedelta(minutes=1),
            last_seen=watermark - timedelta(seconds=45),
            hits=2,
            route="DIRECT",
            last_ingest=507,
        ),
    }
    collector.forget_buckets(None, keep_after=watermark, since_ingest=500)
    collector.reseed_row_watermark(0, 0)
    collector.detach_missing_rows(set())
    kept = {k[4] for k in collector._buckets}
    check("a bucket last touched before the request is dropped", "before.the.request.example" in kept, False)
    check(
        "a bucket touched after it is KEPT even though its clock reads earlier",
        "arrived.during.the.purge.example" in kept,
        True,
    )
    check(
        "and no carried bucket ever claims to have started after it ended",
        all(b.first_seen <= b.last_seen for b in collector._buckets.values()),
        True,
    )

    print()
    print("=== an incomplete delete must never orphan or duplicate a stored row ===")
    collector._buckets = {
        key("row.may.survive.example"): Bucket(
            first_seen=watermark - timedelta(minutes=1),
            last_seen=watermark + timedelta(seconds=10),
            hits=9,
            route="DIRECT",
            row_id=4321,
            flushed_hits=6,
            last_ingest=600,
        )
    }
    collector.forget_buckets(None, keep_after=watermark, since_ingest=500)
    collector.detach_missing_rows({4321})
    survivor = collector._buckets.get(key("row.may.survive.example"))
    check("a bucket whose row really survived keeps pointing at it", survivor.row_id if survivor else None, 4321)
    check("and keeps what it already wrote, so nothing is written twice", survivor.flushed_hits if survivor else None, 6)

    collector._buckets = {
        key("row.was.deleted.example"): Bucket(
            first_seen=watermark - timedelta(minutes=1),
            last_seen=watermark + timedelta(seconds=10),
            hits=9,
            route="DIRECT",
            row_id=4321,
            flushed_hits=6,
            last_ingest=600,
        )
    }
    collector.forget_buckets(None, keep_after=watermark, since_ingest=500)
    collector.reseed_row_watermark(0, 0)
    collector.detach_missing_rows(set())
    orphan = collector._buckets.get(key("row.was.deleted.example"))
    check("a bucket whose row was deleted forgets it, instead of updating nothing", orphan.row_id if orphan else "gone", None)
    check("and counts every hit as unwritten again", orphan.flushed_hits if orphan else None, 0)
    check("so none of its traffic is stranded", orphan.hits if orphan else None, 9)

    print()
    print("=== an age purge that empties the table must reset the row watermark ===")
    collector._buckets = {}
    collector._max_row_id = 900
    collector._ceiling_floor = 800
    collector._seen_ids = {1, 2, 3}
    collector.forget_buckets(watermark)
    collector.reseed_row_watermark(0, 0)
    collector.detach_missing_rows(set())
    check("the row watermark was reset", collector._max_row_id, 0)
    check("the ceiling floor was reset", collector._ceiling_floor, 0)
    check("the identity watch list was reset", collector._seen_ids, set())

    print()
    print("=== but a partial age purge must leave the watermark alone ===")
    collector._max_row_id = 900
    collector._ceiling_floor = 800
    collector.forget_buckets(watermark)
    collector.reseed_row_watermark(900, 5)
    collector.detach_missing_rows(set())
    check("the row watermark survives", collector._max_row_id, 900)
    check("the ceiling floor survives", collector._ceiling_floor, 800)

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
