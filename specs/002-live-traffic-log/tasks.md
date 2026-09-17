# Tasks: Live Traffic Log

**Input**: Design documents from `/specs/002-live-traffic-log/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/api.md, quickstart.md

**Tests**: verification is done by runnable experiments on the test server (quickstart.md), not by unit tests inside the repo, matching the precedent of `specs/001-content-filtering/experiments`.

**Organization**: grouped by user story; five parallel owners — A backend collector/parser/identity, B storage/migration/purge, C API router, D dashboard seam/page/locales, E verification experiments.

## Format: `[ID] [P?] [Story] Description`

## Path Conventions

Panel repository root; dashboard under `dashboard/src`; server-side proofs under `specs/002-live-traffic-log/experiments/` and executed on 1.2.3.4 through `scratchpad/ssh_fleet.sh`.

---

## Phase 1: Setup

- [X] T001 Add `traffic_log_enabled: bool = True (TRAFFIC_LOG_ENABLED)`, `traffic_log_max_records: int = 2_000_000 (TRAFFIC_LOG_MAX_RECORDS)`, `traffic_log_flush_seconds: int = 5 (TRAFFIC_LOG_FLUSH_SECONDS)`, `traffic_log_purge_interval: int = 600 (TRAFFIC_LOG_PURGE_INTERVAL)` to `JobSettings` in `config.py` next to `node_user_usages_retention_days`
- [X] T002 [P] Create package `app/fork/traffic_log/__init__.py` exporting `collector`, `fork_log_stream` (lazy import to avoid import cycles with `app.operation.node`)

---

## Phase 2: Foundational (blocking)

- [X] T003 [P] Owner B: models `TrafficLogRecord`, `TrafficLogIdentity`, `TrafficLogState` in `app/fork/models/traffic_log.py` exactly per data-model.md (`SqliteCompatibleBigInteger` ids; `host String(255) NOT NULL`; `port Integer NOT NULL`; `protocol String(3)`; `refused Boolean default false`; `route String(128)`; `hits Integer default 1`; identities `user_id` PK, `username String(128)`, `admin_id nullable`, `deleted Boolean default false`; state `id` PK always 1, `enabled Boolean default true`); indexes `ix_traffic_log_records_bucket_start`, `ix_traffic_log_records_user_last (user_id,last_seen)`, `ix_traffic_log_records_last_id (last_seen,id)`, `ix_traffic_log_records_node_last (node_id,last_seen)`; no foreign keys
- [X] T004 Owner B: register the three models in `app/fork/models/__init__.py` (`__all__` + `__getattr__`) and in `app/db/models.py::_register_fork_model_tables` (import the module, same shape as content_filter)
- [X] T005 Owner B: Alembic revision `app/db/migrations/versions/a7d5e1f3b294_traffic_log_tables.py` (`down_revision = "f6c4d0e2a183"`) creating the three tables and indexes; `downgrade` drops them in reverse order; upgrade inserts the single `traffic_log_state` row `(1, true, now)`
- [X] T006 [P] Owner A: parser in `app/fork/traffic_log/parse.py`: `parse_access_line(line, node_id, at) -> Event | None` handling `tcp|udp`, bracketed IPv6 hosts, inbound/outbound tags with spaces, `->` and `>>`, numeric `email` → `user_id`, non-numeric → `user_label`; `refused = route == "BLOCK"`; returns None for any other line
- [X] T007 [P] Owner A: identity cache in `app/fork/traffic_log/identity.py`: `resolve_many(ids)` batch query of `users(id, username, admin_id)`, 60 s TTL, marks missing ids `deleted`, upserts `traffic_log_identities` (select-then-insert/update, dialect-neutral), `owner_of(user_id) -> admin_id | None`
- [X] T008 Owner A: collector in `app/fork/traffic_log/collector.py`: `TrafficCollector` with `ensure_attached(node_id, node, name)`, `detach(node_id)`, `start()`/`stop()`, per-node reader task over `node.stream_logs(max_queue_size=5000)` (a `NodeAPIError` item → state `error`, detail, detach), viewer taps (`tap(node_id)` async context manager yielding a 2,000-line queue, oldest dropped, drops counted), live subscribers (`subscribe(user_id=None, node_id=None, admin_id=None)` → 1,000-event queue with drop counter and control messages), bucket map keyed `(user_id, node_id, inbound, host, port, protocol, refused, bucket_start)` capped at 50,000 keys, flush task every `traffic_log_flush_seconds` doing bulk INSERT for new keys and UPDATE by id for existing ones, per-node status per data-model.md, `enabled` flag read from `traffic_log_state` at start and toggled by the API, `available=False` with reason `multi-worker` when `server_settings.workers > 1`; `set_enabled(False)` detaches every node (state `paused`) and `set_enabled(True)` re-attaches healthy nodes; non-sudo subscribers (`admin_id` given) receive only events whose owner is already resolved in the identity cache and equals their `admin_id` — unresolved events are withheld for them and counted, never leaked; new bucket rows are written with ORM `add_all` + `flush` so generated ids come back on SQLite, MySQL and PostgreSQL alike
- [X] T009 Owner A: seam function `fork_log_stream(node_id, node)` in `app/fork/traffic_log/__init__.py` returning `collector.tap(node_id)` factory when attached else `node.stream_logs`
- [X] T010 Owner A: upstream seam edits — `app/operation/node.py::_get_logs_local` returns `fork_log_stream(node_id, node)`; `app/node/worker.py::_start_logs` uses `fork_log_stream(node_id, node)()`; `app/fork/jobs/node_extras.py::after_healthy_node_check` calls `collector.ensure_attached(db_node.id, node, db_node.name)`
- [X] T011 Owner A: `app/fork/jobs/traffic_log_collector.py` with `@on_startup` start (loads `enabled` from DB, attaches healthy nodes from `node_manager.get_healthy_nodes()`), `@on_shutdown` stop, guarded by `runtime_settings.role.runs_node`; add `"traffic_log_collector"` and `"traffic_log_purge"` to `FORK_JOB_MODULES` in `app/fork/jobs/__init__.py`

---

## Phase 3: User Story 1 — Watch what everyone is requesting, right now (P1)

**Goal**: live SSE feed of destination reports for all users, exact-username filter, pause, per-node status, `nodes/logs` gating with non-sudo scoping.

**Independent test**: quickstart steps 1–4 and 11 (live part).

- [X] T012 [P] [US1] Owner C: pydantic schemas in `app/fork/traffic_log/schemas.py` per contracts/api.md (`LiveEvent`, `HistoryItem`, `HistoryPage`, `Summary`, `NodeStatus`, `Status`, `SettingsUpdate`)
- [X] T013 [US1] Owner C: router `app/fork/routers/traffic_log.py` prefix `/api/traffic-log`, `Depends(require_permission("nodes", "logs"))` on every route; `GET /live` SSE (`EventSourceResponse`, params `username?`, `node_id?`; resolve username → user_id through `users` then `traffic_log_identities`; non-sudo passes `admin_id` to `collector.subscribe`; emits `{"control":"paused"}` / `{"control":"unavailable","reason":...}` / `{"control":"dropped","count":n}`; ends when the client disconnects); `GET /status`; `PUT /settings` sudo-only (403 otherwise) persisting `traffic_log_state.enabled` and calling `collector.set_enabled`
- [X] T014 [US1] Owner C: register `("traffic_log", "traffic_log")` in `FORK_ROUTER_MODULES` in `app/fork/routers/__init__.py`
- [X] T015 [P] [US1] Owner D: registry seam in `dashboard/src/fork/registry.ts`: `type ForkStatisticsView = { id, label, icon, permission: { resource, action }, component }`, `registerStatisticsView`, `extraStatisticsViews`
- [X] T016 [US1] Owner D: seam in `dashboard/src/pages/_dashboard.statistics.tsx`: import `extraStatisticsViews` + `hasPermission` filter; widen `viewMode` to `string`; render one `Button` per fork view in the same control row (same classes, `variant` default when active, `aria-pressed`); disable the node `Select` while a fork view is active; render the active fork view's component in place of the charts
- [X] T017 [US1] Owner D: `dashboard/src/fork/pages/statistics-views.ts` registering `{ id: 'traffic-log', label: 'trafficLog.title', icon: Radio, permission: { resource: 'nodes', action: 'logs' }, component: TrafficLogView }` and import it from `dashboard/src/fork/hooks.tsx` next to `./pages/tabs`
- [X] T018 [US1] Owner D: `dashboard/src/fork/pages/traffic-log.tsx` live view: segmented Live/History control, filter bar (username combobox with suggestions from `/api/users?search=&limit=8` via `fetcher`, node `Select`, refused-only toggle), SSE via `EventSource` from `eventsource` with `Authorization` header to `/api/traffic-log/live?username&node_id`, batch flush 150 ms, 500-row cap, pause/resume with pending counter, rows: time (locale), username (`#id` when null), destination host:port, protocol chip, node, inbound, outcome chip (delivered/refused), status strip of per-node chips from `/api/traffic-log/status` polled every 5 s (state colour, dropped count, `no_reports`, `unavailable` reason) plus a banner when `ceiling_active` is true showing `purged_over_ceiling` and `last_purge_at`, sudo-only pause switch calling `PUT /settings`; empty/loading/error states; RTL-safe (`useDirDetection`)
- [X] T019 [P] [US1] Owner D: locale keys `trafficLog.*` in `dashboard/src/fork/locales/en.json` and `fa.json` (Persian title exactly «لاگ زنده ترافیک»), English copies in `ru.json` and `zh.json`
- [X] T020 [P] [US1] Owner E: `specs/002-live-traffic-log/experiments/00_parse.py` — parser checks on the verified lines (`->`, `>>`, `BLOCK`, udp, IPv6 bracket host, tags with spaces, non-numeric email, non-access line → None)
- [X] T021 [US1] Owner E: `specs/002-live-traffic-log/experiments/01_live_e2e.py` — over loopback with the token: status shows node 5 collecting; open `/live`, drive curl through socks 10821/10822 to wikipedia/google/pornhub; assert rows within 3 s with usernames and `refused` for pornhub on the restricted config; reopen with `username=demo-open` and assert exclusivity; open `/api/node/5/logs` concurrently and assert the access lines still arrive there

---

## Phase 4: User Story 2 — Look back over the last two days (P2)

**Goal**: keyset-paged history and summary over any range within 48 h with username/node/inbound/destination/refused filters.

**Independent test**: quickstart step 5 and the history half of step 11.

- [X] T022 [US2] Owner C: `app/fork/traffic_log/service.py` `history(db, admin, start, end, username, node_id, inbound, destination, refused, cursor, limit)` (422 rules from data-model.md, join `traffic_log_identities` for username/`user_deleted`, non-sudo predicate `admin_id == admin.id`, order `last_seen DESC, id DESC`, cursor `<last_seen_iso>|<id>`) and `summary(...)` (connections = sum(hits), destinations = count distinct host, users = count distinct user_id, refused = sum(hits) where refused, top 8 users and hosts)
- [X] T023 [US2] Owner C: `GET /history` and `GET /summary` routes in `app/fork/routers/traffic_log.py`; node names resolved from `nodes` in one query
- [X] T024 [US2] Owner D: history tab in `dashboard/src/fork/pages/traffic-log.tsx`: presets 15m/1h/6h/24h/48h + custom start/end (`datetime-local`), client-side refusal message when start is older than 48 h and server 422 surfaced verbatim, summary cards (connections, destinations, users, refused; top users; top destinations), table with first/last seen, hits, load-more on `next_cursor`, same filter bar as live
- [X] T025 [US2] Owner E: `specs/002-live-traffic-log/experiments/02_history_purge.py` part 1 — after a flush, `/history?username=demo-kids` over the last hour lists the generated destinations with hits ≥ 1 and correct first/last seen; `start=now-3d` → 422 with the two-day message; paging returns no duplicates across two pages with `limit=2`

---

## Phase 5: User Story 3 — Storage stays bounded (P3)

**Goal**: automatic 48 h purge, record ceiling with oldest-first eviction, pause switch, restart resilience.

**Independent test**: quickstart steps 6, 7, 9.

- [X] T026 [P] [US3] Owner B: `app/fork/jobs/traffic_log_purge.py` — `purge_traffic_log()` deleting `bucket_start < now − 48 h` in 5,000-id chunks (≤ 200,000 per run), then `id < max(id) − traffic_log_max_records` the same way; records counters into `collector.purge_stats` (`last_purge_at`, `purged_expired`, `purged_over_ceiling`, `ceiling_active`); scheduled `interval seconds=job_settings.traffic_log_purge_interval`, `id="traffic_log_purge"`, only when `runtime_settings.role.runs_scheduler`
- [X] T027 [US3] Owner C: `/status` exposes the purge counters and `retention_hours: 48`, `max_records`
- [X] T028 [US3] Owner E: `specs/002-live-traffic-log/experiments/02_history_purge.py` part 2 — seed 120 rows with `bucket_start` 3 days old and 80 recent rows on a copy-safe path (test DB), set `TRAFFIC_LOG_MAX_RECORDS=50` for the run, call `purge_traffic_log()` directly, assert expired rows gone, total ≤ 50, the oldest ids gone first, `ceiling_active` true; then reset the env and confirm a normal run deletes nothing recent; finally seed 300,000 recent rows on the test DB and time `/history` first page (< 2 s, SC-003), then delete the seeded rows
- [X] T029 [US3] Owner E: `specs/002-live-traffic-log/experiments/03_pause_restart.sh` — `PUT /settings {"enabled":false}` → status paused, `/live` emits paused control, `/api/node/5/logs` still streams access lines; re-enable → collecting; restart the panel service → status collecting within 60 s

---

## Phase 6: Polish & cross-cutting

- [ ] T030 [P] Owner E: `specs/002-live-traffic-log/experiments/12_owner_only.py` — create a role granting every ordinary permission the panel offers and an administrator holding it; assert that administrator is refused 403 on every traffic-log route (`/status`, `/history`, `/summary`, `/live`, `PUT /settings`, `POST /purge`), that those routes still answer the panel owner 200, and that a route outside the owner-only set still works for them; delete the fixtures afterwards
- [ ] T031 Owner E: re-run `specs/001-content-filtering/experiments/05_real_traffic_matrix.sh` with collection active; record 7/7 in `specs/002-live-traffic-log/experiments/results.md` together with every other experiment's output
- [X] T032 Owner D: `cd dashboard && bun node_modules/typescript/bin/tsc --noEmit -p tsconfig.app.json` — normalised diff against the 578-error baseline must introduce 0; `bun run build` succeeds
- [X] T033 Owner B: `ruff check app config.py` clean; rehearse `alembic upgrade head` + `downgrade -1` + `upgrade head` on a copy of the test DB before running on `/root/dev/panel`
- [ ] T034 Deploy to the test server (copy changed files, migrate, rebuild dashboard, restart), run quickstart 1–11, browser check of the tab; write results to `experiments/results.md`
- [ ] T035 Knowledge-graph observations for each step; three-reviewer gate; commit as `free-guy-ir` with no assistant attribution

---

## Verification status

**No verification output is recorded in this repository.** `experiments/results.md` — the only
file the suite writes its evidence into — still says NOT YET CAPTURED: there is no captured
stdout, no exit status, no summary table and no browser check for any of the checks below. An
`[X]` above therefore records that the code for that task exists and was exercised during
development; it is not a citation of a captured, re-readable proof, and nothing in this section
may be quoted as one.

The table lists the check each task still owes. It becomes evidence only once `run_all.sh` has
been run on the test server and its output lands in `experiments/results.md` (T031, T034).

| Tasks | Check that must be captured |
|---|---|
| T001, T002, T006-T011 | `00_parse.py` — line formats, IPv6, spaces in tags, non-numeric email, non-access lines, and the privacy check that no Event field carries the source address; `06_drop_paths.py` — subscriber queue overflow emits a dropped control with a positive count, viewer tap overflow counted, bucket cap holds at 50,000; `10_restart_recovery.py` — collection resumes with `restreams` incremented after a core restart underneath the collector, with no panel restart |
| T003-T005, T026, T027, T033 | `alembic upgrade head` through `a7d5e1f3b294` then `b8e6f2a4c517`, and a downgrade rehearsal on a seeded copy that removes the three traffic-log tables, leaves `users` and the content-filter tables intact, and restores them; `ruff check --no-fix app config.py` clean |
| T012-T014, T022, T023 | `01_live_e2e.py` — events inside the 3 s budget, refusal marking, exact-username filter exclusivity, every contract field present, no source address; `02_history_purge.py` — 422 outside the retention window, keyset paging without duplicates, summary totals, retention purge, ceiling eviction, first page inside the 2 s budget at the ceiling |
| T015-T019, T024 | A browser check against a real login: the Persian tab renders and opens both Live and History with no console errors, and every `trafficLog.*` key resolves in en/fa/ru/zh |
| T020, T021, T025, T028, T029, T030 | `run_all.sh` on the test server, with `00_parse.py`, `01_live_e2e.py`, `05_viewer_parity.py` and `12_owner_only.py` among the steps that must exit zero |
| T032 | The TypeScript gate against the recorded baseline: `introduced: 0` |
| Retention and purge (FR-018, FR-019) | `07_retention_and_purge.py` — the history boundary tracks a retention set through the API, out-of-range values are rejected, the automatic purge honours the window, on-demand purge works by age and in full, and nothing is resurrected by the next flush; `08_retention_across_processes.py` — a separate process whose collector never started still honours a retention set through the API; `09_disk_reclamation.py` — a capped purge reports `incomplete`, repeating it finishes the job, and reclamation returns bytes to the filesystem under concurrent readers and a writer |

## Dependencies

- Phase 2 blocks every story. Within Phase 2: T003 → T004 → T005 (Owner B chain); T006, T007 → T008 → T009 → T010 → T011 (Owner A chain); the two chains run in parallel.
- US1 (T012–T021) needs Phase 2; C's T013 needs A's T008/T009 signatures (agreed in this file); D's T015–T019 need nothing from A/B/C except the contract; E's T021 needs the deployed backend.
- US2 (T022–T025) needs T003 (models) and T014 (router registered); independent of US3.
- US3 (T026–T029) needs T003; independent of US2.
- Phase 6 needs everything.

## Parallel execution

- Batch 1 (foundational): Owner A [T006, T007, T008, T009, T010, T011] ∥ Owner B [T003, T004, T005, T026] ∥ Owner D [T015, T016, T017, T018, T019, T024] ∥ Owner E [T020 and the scripts for T021, T025, T028, T029, T030 written against the contract].
- Batch 2 (after A+B land): Owner C [T012, T013, T014, T022, T023, T027].
- Batch 3: deploy (T034), run E's scripts, T031–T033, then T035.

## Implementation strategy

MVP = Phase 2 + US1: the operator sees live destinations with the username filter and the status strip. US2 and US3 add history and bounded storage; they are independent of each other and can be verified separately with the scripts above.
