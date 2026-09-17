# API Contract: `/api/traffic-log`

Every route requires an admin token with permission `nodes` / `logs` **and** full panel access (`is_owner`). Any other administrator — including one holding every other permission the panel offers — is refused with 403 and `detail` `only an admin with full panel access can use the traffic log`. Errors use the panel's standard `{ "detail": ... }` shape.

## GET /api/traffic-log/live  (Server-Sent Events)
Query: `username?` (exact), `node_id?`.
Each message is one JSON event:
```json
{"at":"2026-09-15T14:35:37.412Z","user_id":72,"username":"demo-kids","node_id":5,"node":"filter-test-xray","inbound":"Shadowsocks TCP","host":"www.pornhub.com","port":443,"protocol":"tcp","route":"BLOCK","refused":true}
```
A control message `{"control":"dropped","count":N}` is sent when the subscriber queue overflowed; `{"control":"paused"}` when collection is disabled; `{"control":"unavailable","reason":"multi-worker"}` when the collector cannot run. Unknown user → `"username": null`, dashboard shows `#72`.

## GET /api/traffic-log/history
Query: `start` (ISO), `end` (ISO), `username?`, `node_id?`, `inbound?`, `destination?` (substring on host), `refused?` (bool), `cursor?`, `limit?` (1–200, default 100).
422 when `start` is older than the configured retention (`detail`: "history is kept for <hours> hours; choose a start within that window") or when `end ≤ start` (`detail`: "the end of the range must come after its start").
```json
{"items":[{"id":9123,"bucket_start":"…","first_seen":"…","last_seen":"…","hits":6,"user_id":72,"username":"demo-kids","user_deleted":false,"node_id":5,"node":"filter-test-xray","inbound":"Shadowsocks TCP","host":"www.wikipedia.org","port":443,"protocol":"tcp","route":"DIRECT","refused":false}],"next_cursor":"2026-09-15T14:30:00.000Z|9100"}
```
Ordered by `last_seen DESC, id DESC`; `next_cursor` null on the last page.

## GET /api/traffic-log/summary
Same filters as history minus `cursor`/`limit`.
```json
{"connections":1234,"destinations":88,"users":12,"refused":40,"top_users":[{"user_id":72,"username":"demo-kids","hits":300}],"top_destinations":[{"host":"www.google.com","hits":120,"refused":0}]}
```
Top lists are limited to 8 entries each.

## GET /api/traffic-log/status
```json
{"enabled":true,"available":true,"reason":null,"retention_hours":48,"max_records":2000000,"ceiling_active":false,"purge_incomplete":false,"last_purge_at":"…","purged_expired":0,"purged_over_ceiling":0,"nodes":[{"node_id":5,"node":"filter-test-xray","state":"collecting","since":"…","lines":420,"events":390,"dropped":0,"records":57,"last_event_at":"…","detail":null}]}
```

## PUT /api/traffic-log/settings
Body `{"enabled"?: bool, "retention_hours"?: int}` — at least one of the two is required (422 otherwise), and `retention_hours` must be 1–720. → 200 with the new `status` payload.

## POST /api/traffic-log/purge  (owner only, like every route here, and destructive)
Deletes stored records immediately instead of waiting for the scheduled cycle. The body is optional; sending none, or `{}`, purges **every** stored record.
Body:
- `older_than_hours` (int ≥ 0, optional): purge only records whose bucket started more than this many hours ago, clamped to 87,600 (ten years). Omitted or `null` → purge everything.
- `reclaim` (bool, default `false`): after the delete, ask the database to return the freed space to the filesystem. Only SQLite is compacted (WAL checkpoint, `VACUUM`, WAL checkpoint); on MySQL and PostgreSQL the panel reports that nothing was reclaimed because those engines reuse freed pages themselves. Skipped when the delete came back incomplete.

```json
{"removed":12034,"incomplete":false,"remaining":0,"retention_hours":48,"reclaimed":true,"freed_bytes":84129416}
```

`removed` is how many rows this call deleted; `incomplete` is true when the chunked delete hit its 200,000-row cap and more rows still match, in which case the call can simply be repeated; `remaining` is the number of rows left in the table when the call answered; `retention_hours` is the window in force at that moment; `reclaimed` says whether the database was actually compacted, and `freed_bytes` how many bytes went back to the filesystem — `null` when nothing was compacted or the size could not be measured.

The deletion is permanent and there is no undo, so the dashboard puts a confirmation step in front of it (FR-018).

## Username suggestions
Reuse upstream `GET /api/users?search=<prefix>&limit=8` (already admin-scoped). No new endpoint.
