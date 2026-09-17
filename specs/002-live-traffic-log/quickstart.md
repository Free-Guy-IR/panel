# Quickstart: proving the Live Traffic Log on the test server

Prerequisites: the test panel at `/root/dev/panel` (port 8001 behind nginx on 80/8080), node 5 `filter-test-xray` connected, demo users `demo-kids` (filtered) and `demo-open`, admin token at `/root/dev/.filter_token`, all reached through `scratchpad/ssh_fleet.sh 1.2.3.4`. Every authenticated call goes to `http://127.0.0.1:8001` on the server itself.

1. **Migrate + restart**: `cd /root/dev/panel && uv run alembic upgrade head`, restart the panel service, then `GET /api/traffic-log/status` → `available: true`, node 5 `collecting` within 60 s (SC-006, FR-011).
2. **Live feed** (`experiments/01_live_e2e.py`): open `/api/traffic-log/live` over loopback, drive `curl --socks5-hostname` through the restricted (10821) and open (10822) configs to wikipedia, google, pornhub; expect rows with `username` `demo-kids` / `demo-open` within 3 s, `refused: true` for pornhub on the restricted config (SC-001, FR-002/003).
3. **Username filter**: reopen `/live?username=demo-open`; only that user's rows arrive (SC-002, FR-004).
4. **Viewer parity**: while collecting, open `/api/node/5/logs` and confirm the same access lines still arrive there (FR-015, SC-008).
5. **History** (`experiments/02_history_purge.py`): after the flush, `GET /history?start=now-1h&end=now&username=demo-kids` lists the destinations with `hits` and first/last seen; a `start` older than the configured retention → 422 naming that retention (FR-006/007/008).
6. **Purge + ceiling**: seed rows with `bucket_start` three days old and set `TRAFFIC_LOG_MAX_RECORDS=50`, run the purge job function directly; expired rows gone, total ≤ 50, oldest removed first, `ceiling_active` true (SC-004/005, FR-009/010).
7. **Pause**: `PUT /settings {"enabled": false}` as sudo → status `paused`, `/live` emits `{"control":"paused"}`, `/api/node/5/logs` still streams; re-enable → collecting again (FR-013).
8. **Owner-only** (`experiments/12_owner_only.py`): create a role granting every ordinary permission the panel offers and an administrator holding it; every traffic-log route — `/status`, `/history`, `/summary`, `/live`, `PUT /settings`, `POST /purge` — answers that administrator 403 while the same routes still answer the owner 200; the temporary role and administrator are deleted again (SC-007, FR-014).
9. **Restart**: restart the panel; within 60 s status shows node 5 collecting again (SC-006).
10. **No traffic impact**: re-run `specs/001-content-filtering/experiments/05_real_traffic_matrix.sh` — 7/7 rows unchanged (SC-008).
11. **Dashboard**: log in at `http://1.2.3.4/dashboard/`, Statistics → «لاگ زنده ترافیک»; verify live rows, pause counter, username combobox, history presets, refusal message for a >48 h start, status strip; `tsc --noEmit` diff against the 578 baseline = 0 introduced.
