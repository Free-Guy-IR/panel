# Research: Live Traffic Log

Every item below was measured on the test fleet (1.2.3.4) or read from the repository on 2026-09-15 unless marked otherwise.

## 1. What a node reports per connection

- **Decision**: parse the xray access line only.
- **Evidence**: with core 1 at `loglevel: warning` and no `access` key, the node's log stream delivered `2026/09/15 14:35:37 from 1.2.3.4:6746 accepted tcp:www.wikipedia.org:443 [Shadowsocks TCP -> DIRECT] email: 72`, `… accepted tcp:www.google.com:443 [Shadowsocks TCP >> DIRECT] email: 72`, `… accepted tcp:www.pornhub.com:443 [Shadowsocks TCP -> BLOCK] email: 72`. `->` means a routing rule matched, `>>` means the default outbound. The node classifies these lines itself (`backend/xray/log.go::isAccessLog`: contains `from `, ` email:`, ` accepted tcp:`/` accepted udp:`). Inbound and outbound tags may contain spaces; hosts may be IPv6 in brackets.
- **`email`** is `user.get("email") or user.get("id")` (`app/node/sync.py:41`) and arrived as the numeric user id (`72` = `demo-kids`). The parser accepts an integer id; anything else is kept as an opaque label and resolved by username when possible.
- **Refused** = outbound tag equals `BLOCK`, the tag the content-filter feature uses for its discard route (`app/fork/content_filter/rules.py::BLOCK_OUTBOUND`).
- **sing-box nodes**: the fork's sing-box backend (`backend/singbox/log.go`) forwards every line without classification; the test sing-box container printed no connection lines in six hours (only gRPC access lines). Sing-box destination reports are therefore out of this version: their lines pass through to the viewer, and such a node shows "no destination reports".
- **Alternatives considered**: enabling xray's `access` file log and tailing it on the node (needs a node change and a core config change on every node → violates Fleet Safety); xray stats API (no destinations).

## 2. One shared channel per node

- **Decision**: the collector is the sole reader; the per-node viewer reads a tap.
- **Evidence**: `controller/rpc/log.go::GetLogs` reads `backend.Logs()`, which returns the same buffered channel to every caller (`backend/*/...go`, capacity `LOG_BUFFER_SIZE` = 10,000, non-blocking send drops on full). Two gRPC readers split the lines. Panel side, `/api/node/{id}/logs` → `NodeOperation._get_logs_local` → `node.stream_logs` (`app/operation/node.py:1002`), and the split-role worker path `NodeWorkerService._start_logs` (`app/node/worker.py:283`) calls `node.stream_logs()` directly.
- **Seam**: `fork_log_stream(node_id, node)` returns a context-manager factory with the same shape as `node.stream_logs`; it yields a tap queue when the collector is attached to that node and `node.stream_logs` otherwise. Both upstream call sites become one call.
- **Alternatives considered**: changing the node to broadcast (correct long-term, but every production node would need an update before the viewer stopped stealing lines; kept as a follow-up); letting the viewer and the collector both read (silent loss on both sides — rejected).

## 3. Volume and bounds

- **Decision**: size by explicit bounds, count drops, display them.
- **Evidence**: production runs ~5,000 customers on ~24 nodes; the only node containers reachable read-only (`node`, `node-singbox`, `node-mtproto` on the panel host) printed 0 access lines in 60 s — they are idle, so the per-user rate could not be measured this session.
- **Bounds**: node buffer 10,000 lines (given); bridge queue `max_queue_size` 5,000 per node; live subscriber queue 1,000 events (drop oldest, count); viewer tap 2,000 lines (drop oldest, count); bucket map 50,000 keys per flush window (excess counted as dropped); flush every 5 s; record ceiling 2,000,000 (≈ 120 B/row + ~60 % index ≈ 400 MB worst case); retention 48 h.
- **Alternatives considered**: storing raw events (unbounded growth at unknown rate — rejected); minute buckets (5× rows for no operator value — rejected); hourly buckets (loses "when" precision the history view needs — rejected; five minutes keeps first/last seen exact inside the bucket).

## 4. Where the collector runs

- **Decision**: in the process that owns node connections, started from a fork job module at startup, attached per node from the `after_healthy_node_check` fork hook.
- **Evidence**: `Role.ALL_IN_ONE` runs panel + node + scheduler in one process; production sets neither `ROLE` nor `UVICORN_WORKERS` (checked read-only with `docker exec … env`), so `workers` = 1. `app/jobs/node_checker.py:165,183` call `after_healthy_node_check(node, db_node)` for every healthy node on each check (interval `JOB_CORE_HEALTH_CHECK_INTERVAL` = 10 s), which is exactly the re-attach signal needed after a node reconnects.
- **Guard**: `server_settings.workers > 1` → the collector does not start; status = `unavailable (multi-worker)`; the viewer falls back to the direct stream.
- **Alternatives considered**: NATS fan-out for multi-worker (deprecated roles, not in use — deferred).

## 5. Storage shape and dialect neutrality

- **Decision**: three tables (`traffic_log_records`, `traffic_log_identities`, `traffic_log_state`), plain INSERT/UPDATE, keyset paging, chunked deletes.
- **Evidence**: the panel supports SQLite, MySQL and PostgreSQL; `app/fork/jobs/cleanup_node_user_usages.py` already implements the portable select-ids-then-delete loop (`DELETE_CHUNK` 5,000, `MAX_PER_RUN` 200,000) and registers through `FORK_JOB_MODULES`. `SqliteCompatibleBigInteger` is the id type used by fork tables.
- **Ceiling without COUNT(*)**: `max(id) − ceiling` as the delete threshold; with gaps it deletes slightly more than necessary, never less.
- **Alternatives considered**: dialect-specific upserts (three code paths — rejected); FK to `users` with cascade (would erase history the moment a user is deleted, contradicting the spec — rejected; identities keep the last known name instead).

## 6. Access control

- **Decision**: reuse `nodes`/`logs`.
- **Evidence**: `app/models/admin_role.py::NodesPermissions.logs`; enforced for the raw viewer at `app/routers/node.py:322` via `require_permission_for_request(request, db, token, "nodes", "logs")`. The raw viewer already exposes these very lines to any admin with that permission, so the feature never widens exposure; it narrows it, because the shipped traffic log additionally requires full panel access and refuses every other administrator with 403.
- **Username suggestions** reuse `GET /api/users?search=` which is already scoped per admin.

## 7. Dashboard seam and UI building blocks

- **Decision**: a `statistics-view` registry slot rendered by the Statistics page; the view reuses the node-logs page's SSE pattern.
- **Evidence**: `_dashboard.statistics.tsx` keeps a `viewMode` state and renders two toggle buttons; no tab primitive. The registry already offers `registerComponent(slot, id)`. The node logs page (`_dashboard.nodes.logs.tsx`) opens `EventSource` from the `eventsource` package with an `Authorization` header and batches messages with a flush timer; `terminal-line.tsx` shows the row style used for logs. The sidebar seam (`import { forkMainNavItems }` + one spread) is the precedent for the edit size allowed in an upstream page.

## 8. Timestamps

- **Decision**: panel receipt time (UTC) on every event and record; the dashboard renders in the browser's locale.
- **Rationale**: node clocks are not guaranteed to agree; a single clock keeps cross-node ordering consistent.
