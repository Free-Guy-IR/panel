# Implementation Plan: Live Traffic Log

**Branch**: `feat/content-filtering` (spec dir `specs/002-live-traffic-log`) | **Date**: 2026-09-15 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/002-live-traffic-log/spec.md`

## Summary

Add a "لاگ زنده ترافیک" view to the Statistics page. A collector inside the all-in-one panel process becomes the sole reader of each connected node's log stream, passes every raw line on to the existing per-node log viewer, parses the destination-report lines (`from … accepted tcp|udp:host:port [inbound -> outbound] email: id`), fans the parsed events out to a live SSE feed and folds them into five-minute per-destination records. Records live for a retention window stored in the database, 48 hours by default and settable between 1 and 720 hours, under a hard record ceiling; both are enforced by a chunked scheduled job. An owner-only API — `nodes/logs` plus full panel access — serves the live feed, a keyset-paged history, a summary, per-node collection status, a pause switch, the retention setting and an on-demand purge. The dashboard gains a registry seam for Statistics views and a professional live/history tab.

## Technical Context

**Language/Version**: Python ≥ 3.14 (panel), TypeScript 5 / React 19 via Bun (dashboard)

**Primary Dependencies**: FastAPI, SQLAlchemy 2 async, Alembic, apscheduler, `sse-starlette` (already used by `/api/node/{id}/logs`), PasarGuardNodeBridge (`node.stream_logs`), TanStack Query, `eventsource` package (already used by the node logs page), shadcn/ui primitives already in the tree

**Storage**: the panel database (SQLite on the test server, MySQL in production, PostgreSQL supported upstream) — three new fork tables via an Alembic revision on head `f6c4d0e2a183`, plus a follow-up revision adding `traffic_log_state.retention_hours`

**Testing**: experiment scripts run on the test server over ssh + loopback (same shape as `specs/001-content-filtering/experiments`), the existing traffic matrix, `tsc --noEmit` against the recorded 578-error baseline, ruff

**Target Platform**: Linux server, single all-in-one panel process (verified: production has no `UVICORN_WORKERS`/`ROLE` override)

**Project Type**: web service + SPA dashboard (fork seams only)

**Performance Goals**: a destination appears in the live feed ≤ 3 s after the node reports it; history first page ≤ 2 s at the record ceiling; collector overhead bounded so node syncing and the API are unaffected

**Constraints**: node log capture is best-effort (10,000-line node buffer, non-blocking); one shared channel per node; no node config change; no core restart; retention 48 h by default and settable between 1 and 720 hours; record ceiling default 2,000,000; no source IP stored; no explanatory comments; no new TS errors

**Scale/Scope**: ~5,000 customers, ~24 nodes in production; per-user connection rate unmeasured, so every buffer in the pipeline is bounded and drops are counted and displayed

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | How this design satisfies it |
|-----------|------------------------------|
| I. Upstream Boundary | All logic under `app/fork/traffic_log/`, `app/fork/routers/traffic_log.py`, `app/fork/jobs/traffic_log_*.py`, `app/fork/models/traffic_log.py`, `dashboard/src/fork/pages/traffic-log.tsx`. Upstream touched only at seams: one-line stream substitution in `app/operation/node.py::_get_logs_local` and `app/node/worker.py::_start_logs`, the existing `after_healthy_node_check` fork hook, the fork model/route/job registries, and a new `registerStatisticsView` seam in `_dashboard.statistics.tsx` (import + spread, like the sidebar). Route prefix `/api/traffic-log` collides with nothing; table names are prefixed `traffic_log_`. |
| II. Verified Ground | Line format, `email` = numeric id, single shared channel, `>>`/`->` semantics, `BLOCK` outbound tag, single-process production, `nodes/logs` permission all measured (research.md). The one unmeasurable — per-user rate — is replaced by explicit bounds. |
| III. Test Server First | Built and proven on 1.2.3.4 only; production read once, read-only, for the worker/role facts. |
| IV. Fleet Safety | No node config change, no core restart, no user re-push. Collector restarts with the panel and re-attaches through the healthy-node hook. Every failure state (stream error, buffer full, ceiling reached, multi-worker) is displayed, never hidden. Memory and storage are capped by stated numbers. |
| V. Clean Code | No comments; `tsc` diff against the 578 baseline; ruff; reversible migration rehearsed on a copy of the test DB. |
| VI. Security and Privacy | `nodes/logs` permission **and** full panel access on every endpoint, so a non-owner is refused with 403 rather than scoped; no source IP stored; retention purge plus ceiling; tokens only over loopback/ssh in tests. |
| VII. Independent Review and Record | Three reviewers at the end; KG entity created and updated per step; fork identity on commits. |

**Gate result (pre-research)**: PASS — no violations to justify.

## Project Structure

### Documentation (this feature)

```text
specs/002-live-traffic-log/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/api.md
├── experiments/            # runnable proofs, executed on the test server
└── tasks.md                # /speckit-tasks output
```

### Source Code (repository root)

```text
app/fork/traffic_log/
├── __init__.py             # fork_log_stream(node_id, node) seam entry, collector singleton
├── parse.py                # access-line parser → Event
├── collector.py            # per-node reader tasks, viewer taps, live fan-out, bucket aggregation, flush
├── identity.py             # user id → (username, admin_id) cache + identities upsert
├── service.py              # history/summary/status/settings queries
└── schemas.py              # pydantic response/request models
app/fork/models/traffic_log.py          # TrafficLogRecord, TrafficLogIdentity, TrafficLogState
app/fork/routers/traffic_log.py         # /api/traffic-log/*
app/fork/jobs/traffic_log_collector.py  # startup/shutdown wiring + healthy-node attach
app/fork/jobs/traffic_log_purge.py      # retention + ceiling, chunked
app/db/migrations/versions/a7d5e1f3b294_traffic_log_tables.py
app/db/migrations/versions/b8e6f2a4c517_traffic_log_retention.py
config.py::JobSettings                   # TRAFFIC_LOG_* env fields (precedent: NODE_USER_USAGES_RETENTION_DAYS)

dashboard/src/fork/registry.ts          # registerStatisticsView / extraStatisticsViews
dashboard/src/fork/pages/traffic-log.tsx
dashboard/src/fork/pages/statistics-views.ts
dashboard/src/fork/locales/{en,fa,ru,zh}.json  # trafficLog.* keys
dashboard/src/pages/_dashboard.statistics.tsx  # seam: fork view buttons + content

Seams touched in upstream files (one call each):
app/operation/node.py::_get_logs_local     → return fork_log_stream(node_id, node)
app/node/worker.py::_start_logs            → fork_log_stream(node_id, node)()
app/fork/jobs/node_extras.py::after_healthy_node_check → collector.ensure_attached(...)
app/fork/routers/__init__.py, app/fork/jobs/__init__.py, app/fork/models/__init__.py, app/db/models.py::_register_fork_model_tables
```

**Structure Decision**: fork-package layout identical to `app/fork/content_filter/`, so the two features read the same way; the dashboard view lives beside `content-filter.tsx` and is mounted through a new registry slot rather than a route, because the operator asked for a button in the Statistics control row, not a page.

## Design Decisions (summary; details in research.md)

1. **Sole reader + taps.** The collector owns `node.stream_logs()` per node. The per-node viewer gets a tap (bounded queue, oldest dropped, drop count kept) through `fork_log_stream`, which falls back to the direct stream whenever the collector is not attached (disabled, paused, multi-worker, node not yet attached). No line is lost to the viewer that the collector saw.
2. **Parse-then-fan-out.** Non-matching lines only go to taps. Matching lines become an `Event` (receipt time, user id, node id, inbound, host, port, protocol, route, refused = route == `BLOCK`) that is pushed to live subscribers (bounded per-subscriber queues) and folded into the current five-minute bucket map.
3. **Bounded aggregation.** Bucket map capped at 50,000 keys per flush window; beyond that events are counted as dropped. Flush every 5 s: bulk INSERT for new keys, UPDATE by id for keys already written this bucket. Dialect-neutral (no upsert syntax).
4. **Identity.** `email` is parsed as an int; the cache resolves ids in batch at flush time from `users` and upserts `traffic_log_identities` (last known username, admin_id). Live rows use the cache; unknown ids show `#id` until resolved.
5. **Retention + ceiling.** Purge job every 10 min: delete `bucket_start < now − the configured retention` in 5,000-id chunks, up to 200,000 rows per run; then delete `id <= max(id) − ceiling` in the same chunks, uncapped in rows but bounded by a 60-second time budget. `max−ceiling` over-estimates the count only in the safe direction.
6. **Access.** Every route sits behind `require_permission("nodes", "logs")` and then an owner check; an administrator who clears the permission but is not the panel owner gets 403, so there is no per-admin row scoping left to get wrong.
7. **Multi-worker guard.** `server_settings.workers > 1` → collector never starts; status reports `unavailable` with the reason; viewer falls back to the direct stream.
8. **Dashboard seam.** `registerStatisticsView({ id, label, icon, permission, component })`; the Statistics page renders fork buttons in the control row and the active fork view in place of the charts. Live tab: SSE with Authorization header (same as node logs page), 500-row cap, pause with pending counter, username combobox fed by `/api/users?search=` (already admin-scoped), node filter, status strip. History tab: presets + custom range clamped by refusal at 48 h, keyset "load more", summary cards.

## Complexity Tracking

No constitution violations; table intentionally empty.

## Constitution Check (post-design)

Re-evaluated after data-model and contracts: PASS. The only upstream edits are three one-call seams and one import+spread in the Statistics page, matching the precedent of the sidebar seam.
