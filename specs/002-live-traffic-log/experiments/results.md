# Raw experiment output — Live Traffic Log

**Status: NOT YET CAPTURED.**

What is recorded here: nothing. No captured stdout, no exit status, no summary table and no
browser check. Every line below describes what a run *must* produce, not what a run did
produce.

What is recorded elsewhere: the code. Every experiment named below exists under
`experiments/`, and the panel behaviour they exercise exists under `app/fork/`. Existing code
is not a passing run.

This file is the record `run_all.sh` overwrites from scratch. Until that script has been
executed on the test server and its output lands here, nothing about this suite may be quoted
as evidence anywhere — `tasks.md` included — and T031 and T034 stay open.

## How to produce it

```
cd /root/dev/panel
bash specs/002-live-traffic-log/experiments/run_all.sh
```

`run_all.sh` writes this file from scratch: one `##` section per step holding that step's
verbatim stdout and its exit status, then a summary table.

The table below lists the criterion-bearing steps and the evidence each must carry. It is not
the full run order: `run_all.sh` is the authority on that, and it also runs
`16_filter_fixture.py`, `11_bucket_key.py`, `13_cleanup_faults.py`, `14_single_reader.py`,
`15_purge_race.py`, `06_drop_paths.py`, `07_retention_and_purge.py`,
`08_retention_across_processes.py` and `09_disk_reclamation.py`, with
`10_restart_recovery.py` behind `RUN_RESTART_RECOVERY=1` because it restarts a core. The 001
matrix runs last, because it brings up and tears down its own copies of the demo socks proxies
on 10821/10822 that every 002 experiment depends on.

| order | step | evidence it must carry |
|---|---|---|
| 1 | `proxies.sh up` | `demo socks proxies listening: 2/2` |
| 2 | `00_parse.py` | `ALL parse CHECKS PASSED` (FR-003, FR-016) |
| 3 | `01_live_e2e.py` | `ALL live-e2e CHECKS PASSED`; per-row latency under 3 s (SC-001), `username=demo-open` exclusivity (SC-002), the raw viewer still showing the access lines |
| 4 | `05_viewer_parity.py` | `ALL viewer-parity CHECKS PASSED`; the attached/detached line-count and per-probe access-line comparison printed under `SC-008: attached versus detached` |
| 5 | `04_controls.py` | `ALL control-message CHECKS PASSED`; a `{"control":"dropped","count":N}` message read back from a stalled subscriber (FR-012) and the `{"control":"unavailable","reason":"multi-worker"}` message a multi-worker deployment produces |
| 6 | `12_owner_only.py` | `ALL owner-only CHECKS PASSED`; an administrator whose role grants every ordinary permission the panel offers, but who is not the panel owner, refused 403 on `/status`, `/history`, `/summary`, `/live`, `PUT /settings` and `POST /purge`, the same routes still answering the owner 200, a route outside the owner-only set still working for that administrator, and the temporary role and administrator deleted again (SC-007, FR-014) |
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
- `05_viewer_parity.py` pauses and resumes collection, and `12_owner_only.py` creates a
  throwaway role and administrator and deletes both again; each restores what it changed in
  its own cleanup. If a run is interrupted, check `GET /api/traffic-log/status` reports
  `enabled: true` and that no `tl-maximal-*` role or administrator is left behind before
  trusting a later run.
