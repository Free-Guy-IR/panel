import asyncio
import os
import sys
from datetime import UTC, datetime

PANEL_ROOT = os.environ.get("PANEL_ROOT", "/root/dev/panel")
sys.path.insert(0, PANEL_ROOT)

from app.fork.traffic_log import collector as module
from app.fork.traffic_log.collector import (
    BUCKET_CAP,
    SUBSCRIBER_QUEUE_SIZE,
    TAP_QUEUE_SIZE,
    NodeState,
    TrafficCollector,
)

NODE_ID = 9001
failures = []


def check(label, got, want):
    ok = got == want
    print("  %-62s %s" % (label, "OK" if ok else "FAIL got=%r want=%r" % (got, want)))
    if not ok:
        failures.append(label)


def line(host, user_id=72, protocol="tcp", route="DIRECT"):
    stamp = datetime.now(UTC).strftime("%Y/%m/%d %H:%M:%S")
    return f"{stamp} from 10.0.0.9:5555 accepted {protocol}:{host}:443 [Probe Inbound -> {route}] email: {user_id}"


def fresh():
    instance = TrafficCollector()
    instance.available = True
    instance.enabled = True
    state = NodeState(node_id=NODE_ID, name="probe-node")
    instance._states[NODE_ID] = state
    return instance, state


async def main():
    print("=== a live subscriber that stops reading is told lines were dropped (FR-012) ===")
    instance, state = fresh()
    async with instance.subscribe() as queue:
        for index in range(SUBSCRIBER_QUEUE_SIZE + 50):
            instance._ingest(state, line(f"burst-{index}.test"))
        check("the subscriber queue is capped", queue.qsize() <= SUBSCRIBER_QUEUE_SIZE, True)
        check("the node counted live drops", state.dropped_live > 0, True)
        drained = []
        while not queue.empty():
            drained.append(queue.get_nowait())
        controls = [item for item in drained if isinstance(item, dict) and "control" in item]
        dropped = [item for item in controls if item.get("control") == "dropped"]
        check("a dropped control message reached the subscriber", len(dropped) >= 1, True)
        check("the dropped control carries a positive count", all(item.get("count", 0) > 0 for item in dropped), True)
        print("    live drops counted: %d, control messages: %d" % (state.dropped_live, len(controls)))

    print("\n=== a log viewer that stops reading loses lines, and they are counted ===")
    instance, state = fresh()
    async with instance.tap(NODE_ID) as viewer:
        for index in range(TAP_QUEUE_SIZE + 25):
            instance._ingest(state, line(f"viewer-{index}.test"))
        check("the viewer queue is capped", viewer.qsize() <= TAP_QUEUE_SIZE, True)
        check("the node counted viewer drops", state.dropped_viewer > 0, True)
        print("    viewer drops counted: %d" % state.dropped_viewer)

    print("\n=== the bucket map refuses to grow past its cap and counts what it refused ===")
    instance, state = fresh()
    for index in range(BUCKET_CAP + 40):
        instance._ingest(state, line(f"bucket-{index}.test"))
    check("bucket map stayed at its cap", len(instance._buckets) <= BUCKET_CAP, True)
    check("the node counted record drops", state.dropped_records > 0, True)
    check("the aggregate dropped counter reflects them", state.snapshot()["dropped"] >= state.dropped_records, True)
    print("    buckets held: %d, record drops: %d" % (len(instance._buckets), state.dropped_records))

    print("\n=== a subscriber scoped to one admin never receives an unresolved owner ===")
    instance, state = fresh()
    async with instance.subscribe(admin_id=4242) as queue:
        for index in range(20):
            instance._ingest(state, line(f"scoped-{index}.test", user_id=777))
        events = []
        while not queue.empty():
            item = queue.get_nowait()
            if isinstance(item, dict) and "control" not in item:
                events.append(item)
        check("no event leaked to the foreign admin", events, [])
        check("the scoped subscriber received nothing at all", queue.qsize(), 0)

    print("\n=== an unscoped subscriber DOES receive the same events (the filter is the only reason) ===")
    instance, state = fresh()
    async with instance.subscribe() as queue:
        for index in range(20):
            instance._ingest(state, line(f"unscoped-{index}.test", user_id=777))
        events = []
        while not queue.empty():
            item = queue.get_nowait()
            if isinstance(item, dict) and "control" not in item:
                events.append(item)
        check("the unscoped subscriber received every event", len(events), 20)
        check("each event names the destination", all(e["host"].startswith("unscoped-") for e in events), True)

    print("\n=== counters survive a status snapshot round trip ===")
    snapshot = state.snapshot()
    for key in ("dropped", "dropped_live", "dropped_viewer", "dropped_records", "stream_full", "restreams", "records"):
        check(f"snapshot carries {key}", key in snapshot, True)


asyncio.run(main())
print()
if failures:
    print("FAILED %d check(s): %s" % (len(failures), failures))
    raise SystemExit(1)
print("ALL drop-path CHECKS PASSED")
