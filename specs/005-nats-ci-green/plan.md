# Implementation Plan: NATS Suite Green Without Skips

**Branch**: `p5-2/nats-green` | **Date**: 2026-10-06 | **Spec**: specs/005-nats-ci-green/spec.md

## Summary

One product fix and two upstream test fixes, plus the four nats-loop harness commits cherry-picked from `p5-2/nats-loop`:

1. `app/nats/kv_watch.py`: the live phase moves into `_subscribe_live()`, and queue items carry their stream revision. The iterator drops any revision that is not newer than the last one delivered. It checks the client's reconnect counter on every loop, and at most once per `RECONNECT_CHECK_INTERVAL` (1 s) while idle. After a reconnect it closes the live subscription and opens a new one at the last delivered revision + 1.
2. `tests/test_nats_sync_integration.py::test_health_poll_recovers_claim_that_expires_after_startup`:
   - It waits for the worker's refused claim instead of waiting for the worker to exit.
   - It then stops that worker with the bridge's own `_cleanup_sync_worker()` and expires the lease, so the health poll has to start a new worker.
   - It stubs `sync_users_chunked`, the call the bridge actually makes for node 0.5.4, and waits for that delivery.
3. `tests/node_delivery_process_worker.py` and `tests/test_node_full_sync_processes.py`: before it prints READY, each child subscribes to `<bucket>.wake.<node_id>` and calls `wake_sync_worker()` on every message. All three process tests publish that message once their work is queued, because the child now subscribes before READY and its first claim can come before the parent's enqueue. The print of the nonexistent `node._worker_progress` is removed.
4. `tests/test_node_sync.py`: int's 2575f15e0 carries this adaptation. The branch, rebased onto int f4653fd0c, leaves the file alone.
5. F1/F2 (2026-10-07): tests/test_nats_watch_lost_event.py.
   - New `test_a_lost_resubscribe_reply_rebuilds_the_index_without_losing_a_key`: the proxy drops the reply to the resubscribe's `$JS.API.CONSUMER.CREATE`. One case then kills the connection; the other keeps it. The test checks that KvKeyIndex rebuilt, converged to the exact key set and follows later writes, and that the orphan consumer expires after `inactive_threshold`, which the test sets to 3 s.
   - `writes-across-reconnect` gains a quiet-recovery wait and a second cut.
   - The blocked-reader case is renamed `blocked-reader-dedup`.

## Technical Context

Python 3.14, nats-py 2.16.0 (pinned in uv.lock), nats-server 2.10.25 for the local run, pytest-asyncio. The bridge is 6116922, 0.9.2+singbox, which the uv.lock of the base, panel main 3b10bde7f, pins; this branch changes neither uv.lock nor pyproject.toml.

## Constitution Check

- I. Upstream Boundary: the defect is inside the upstream file `app/nats/kv_watch.py`, so it is fixed there and nowhere else, and the change is listed in OVERRIDES.md. No fork seam is needed for a bug fix. The changes to the two upstream test files are recorded the same way. All three are upstream candidates (item C4).
- II. Verified Ground: each root cause was reproduced on the real system before any design (see research below).
- III. No server is touched; nothing is deployed.
- V. No comments or docstrings are added; ruff check and ruff format stay clean on the touched files.
- VII. A 3/3 reviewer gate on the exact commit, and a KG entry for each step.

## Research (measured)

- nats-py 2.16.0 `_JSI.check_for_sequence_mismatch` returns early while `_cmeta` is None, so a heartbeat cannot reveal a lost first delivery. Its data-path reset restarts at `_sseq + 1`, where `_sseq` starts at 0, so a reset before the first message replays the whole stream.
- `PushSubscription.unsubscribe()` cancels the subscription's callback task. A `Queue.put` that is waiting is then cancelled and enqueues nothing.
- The upstream bridge (bec9cb2 and 6116922) defines `Controller._deliver_claimed_users`, which calls `self.sync_users_chunked` for nodes at 0.2.0 or later. The base Controller has neither `sync_users_chunked` nor `_sync_batch_users`.
- Child processes idle out 0.5 s after connect, and nothing in them reacts to a KV write by another process.

## Verification

- RED: the new tests run against f58433750's `kv_watch.py`, with the fixed tests in place, and must fail.
- GREEN: the same tests pass with the fix.
- Full NATS-on suite three times in a row: `TEST_FROM=local TESTING=1 NATS_ENABLED=1 NATS_URL=nats://127.0.0.1:4362 UVICORN_WORKERS=2 ROLE=all-in-one NATS_SERVER_BINARY=<nats-server 2.10.25> pytest tests/`.
