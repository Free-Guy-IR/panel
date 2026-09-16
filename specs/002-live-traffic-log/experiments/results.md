# Raw experiment output — Live Traffic Log

**Status: NOT YET CAPTURED.** No line of this file has been produced by a run on the test
server. It is the record `run_all.sh` overwrites; until that script has been executed on
1.2.3.4 nothing here may be quoted as evidence, and T031 stays open.

## How to produce it

```
cd /root/dev/panel
bash specs/002-live-traffic-log/experiments/run_all.sh
```

`run_all.sh` writes this file from scratch: one `##` section per step holding that step's
verbatim stdout and its exit status, then a summary table. It runs the steps in the order
below, because the 001 matrix brings up and tears down its own copies of the demo socks
proxies on 10821/10822 that every 002 experiment depends on.

| order | step | evidence it must carry |
|---|---|---|
| 1 | `proxies.sh up` | `demo socks proxies listening: 2/2` |
| 2 | `00_parse.py` | `ALL parse CHECKS PASSED` (FR-003, FR-016) |
| 3 | `01_live_e2e.py` | `ALL live-e2e CHECKS PASSED`; per-row latency under 3 s (SC-001), `username=demo-open` exclusivity (SC-002), the raw viewer still showing the access lines |
| 4 | `05_viewer_parity.py` | `ALL viewer-parity CHECKS PASSED`; the attached/detached line-count and per-probe access-line comparison printed under `SC-008: attached versus detached` |
| 5 | `04_controls.py` | `ALL control-message CHECKS PASSED`; a `{"control":"dropped","count":N}` message read back from a stalled subscriber (FR-012) and the `{"control":"unavailable","reason":"multi-worker"}` message a multi-worker deployment produces |
| 6 | `03_scoping.py` | `ALL scoping CHECKS PASSED`; stage 6 proving a customer with no owning administrator is invisible to a non-sudo admin and visible to sudo (SC-007, FR-014) |
| 7 | `02_history_purge.py` | `ALL history-purge CHECKS PASSED`; part 2's `/status` payload, part 3's ceiling pass on a throwaway database copy with the purge-interval assertion (SC-004/005), part 4's first page under 2 s with the store at its 2,000,000 record ceiling (SC-003) and `ceiling_active: true` read back from `GET /status` (FR-010) |
| 8 | `03_pause_restart.sh` | `ALL pause-restart CHECKS PASSED`; collection back within 60 s of a restart (SC-006), the idle node reaching `no_reports` (FR-011), the raw viewer streaming while paused (FR-013) |
| 9 | `proxies.sh down` | the helper proxies gone |
| 10 | `001/05_real_traffic_matrix.sh` | `TRAFFIC MATRIX: ALL PASSED` with all seven rows, captured while collection was active (SC-008) |

## Notes for whoever runs it

- Part 4 of `02_history_purge.py` seeds 2,000,000 rows into the panel's own database to
  reach the record ceiling, and while they are in place the panel's purge starts evicting
  the oldest records — that eviction is the FR-010 behaviour the part proves, and the
  seeded rows are deleted again on the way out. Run the whole suite, or that experiment,
  last. `--skip-load` skips the part, and `--load-rows=N` measures SC-003 below the
  ceiling, in which case the script fails the "measured at or above its ceiling" check and
  says so rather than claiming SC-003.
- Part 3 of the same script never touches the panel's database: `run_all.sh` hands it a
  throwaway SQLite copy through `SQLALCHEMY_DATABASE_URL`, and the phase refuses to run
  without the `TL_DISPOSABLE=1` marker that copy comes with. On a non-SQLite panel, point
  `TL_DISPOSABLE_DB_URL` at a disposable copy first.
- `05_viewer_parity.py` pauses and resumes collection, and `03_scoping.py` moves the owner
  of `demo-open` and deletes a throwaway administrator; both restore what they changed in
  their own cleanup. If a run is interrupted, check `GET /api/traffic-log/status` reports
  `enabled: true` and that `demo-open` still has its original owner before trusting a
  later run.
