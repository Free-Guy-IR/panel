# API Contract: `/api/traffic-log`

All routes require an admin token with permission `nodes` / `logs`. Non-sudo admins are scoped to their own users on every route. Errors use the panel's standard `{ "detail": ... }` shape.

## GET /api/traffic-log/live  (Server-Sent Events)
Query: `username?` (exact), `node_id?`.
Each message is one JSON event:
```json
{"at":"2026-09-15T14:35:37.412Z","user_id":72,"username":"demo-kids","node_id":5,"node":"filter-test-xray","inbound":"Shadowsocks TCP","host":"www.pornhub.com","port":443,"protocol":"tcp","route":"BLOCK","refused":true}
```
A control message `{"control":"dropped","count":N}` is sent when the subscriber queue overflowed; `{"control":"paused"}` when collection is disabled; `{"control":"unavailable","reason":"multi-worker"}` when the collector cannot run. Unknown user → `"username": null`, dashboard shows `#72`.

## GET /api/traffic-log/history
Query: `start` (ISO), `end` (ISO), `username?`, `node_id?`, `inbound?`, `destination?` (substring on host), `refused?` (bool), `cursor?`, `limit?` (1–200, default 100).
422 when `end ≤ start` or `start < now − 48h` (`detail`: "history is kept for 48 hours; choose a start within the last two days").
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
{"enabled":true,"available":true,"reason":null,"retention_hours":48,"max_records":2000000,"ceiling_active":false,"last_purge_at":"…","purged_expired":0,"purged_over_ceiling":0,"nodes":[{"node_id":5,"node":"filter-test-xray","state":"collecting","since":"…","lines":420,"events":390,"dropped":0,"records":57,"last_event_at":"…","detail":null}]}
```

## PUT /api/traffic-log/settings  (sudo only)
Body `{"enabled": false}` → 200 with the new `status` payload. Non-sudo → 403.

## Username suggestions
Reuse upstream `GET /api/users?search=<prefix>&limit=8` (already admin-scoped). No new endpoint.
