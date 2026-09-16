import asyncio
import contextlib
import os
import sys

ROOT = os.environ.get("PANEL_ROOT", "/root/dev/panel")
sys.path.insert(0, ROOT)

failures = []
opened = {"count": 0, "live": 0, "peak": 0}


def check(label, got, want):
    ok = got == want
    print("  %-64s %s" % (label, "OK" if ok else "FAIL got=%r want=%r" % (got, want)))
    if not ok:
        failures.append(label)


class FakeNode:
    def __init__(self):
        self.queues = []

    @contextlib.asynccontextmanager
    async def stream_logs(self, **kwargs):
        opened["count"] += 1
        opened["live"] += 1
        opened["peak"] = max(opened["peak"], opened["live"])
        queue: asyncio.Queue = asyncio.Queue()
        self.queues.append(queue)
        try:
            yield queue
        finally:
            opened["live"] -= 1
            if queue in self.queues:
                self.queues.remove(queue)

    def emit(self, line):
        for queue in list(self.queues):
            queue.put_nowait(line)

    async def get_backend_stats(self):
        raise RuntimeError("no stats in this harness")


async def drain(outbox, sink, stop):
    while not stop.is_set():
        try:
            item = await asyncio.wait_for(outbox.get(), timeout=0.2)
        except TimeoutError:
            continue
        sink.append(item)


async def main():
    from app.fork.traffic_log import fork_log_stream
    from app.fork.traffic_log.collector import collector

    node_id = 90210
    node = FakeNode()
    stream = fork_log_stream(node_id, node)

    print("=== the collector is detached, so the viewers must share one node stream ===")
    check("no collector reader is attached for this node", collector.is_attached(node_id), False)

    first_seen, second_seen = [], []
    stop = asyncio.Event()
    async with stream() as first_box, stream() as second_box:
        readers = [
            asyncio.create_task(drain(first_box, first_seen, stop)),
            asyncio.create_task(drain(second_box, second_seen, stop)),
        ]
        await asyncio.sleep(1.0)
        check("exactly one node stream was opened for two viewers", opened["count"], 1)
        check("and only one was ever open at a time", opened["peak"], 1)

        for index in range(6):
            node.emit("2026/09/16 06:00:0%d from 1.2.3.4:5 accepted tcp:example.com:443 [In -> DIRECT] email: 1" % index)
            await asyncio.sleep(0.05)
        await asyncio.sleep(1.0)
        stop.set()
        for task in readers:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    print("    first viewer received %d lines, second received %d" % (len(first_seen), len(second_seen)))
    check("the first viewer saw every line", len(first_seen), 6)
    check("the second viewer saw every line too", len(second_seen), 6)
    check("neither viewer lost lines to the other", first_seen, second_seen)

    await asyncio.sleep(0.5)
    check("the shared stream closed once the last viewer left", opened["live"], 0)
    check("no direct pump is left running", collector._direct_pumps.get(node_id), None)
    check("no direct reader registration is left behind", collector._direct.get(node_id), None)


if __name__ == "__main__":
    asyncio.run(main())
    print()
    if failures:
        print("FAILED %d check(s): %s" % (len(failures), failures))
        sys.exit(1)
    print("ALL single-reader CHECKS PASSED")
    sys.exit(0)
