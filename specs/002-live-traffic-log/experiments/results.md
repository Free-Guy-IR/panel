# Raw experiment output — Live Traffic Log

Captured verbatim by `run_all.sh` on 2026-09-17 01:31:44 UTC from the test panel.
Every success criterion in `../spec.md` traces to one of the blocks below.

Blocks appear in the order `run_all.sh` lists its steps, which is not the order
they ran in, and a step that waits on a scheduled job prints its parts out of
order too. Read the timestamps inside a block rather than its position: in
`02_history_purge.py`, for example, a `last_purge_at` of 01:51:12 is printed
before one of 01:41:11.

| step | covers |
|---|---|
| 00_parse.py | FR-003, FR-016 — the access-line parser and the absence of any source address |
| 01_live_e2e.py | SC-001, SC-002, FR-002/003/004, the raw viewer while collecting |
| 05_viewer_parity.py | SC-008 — the per-node viewer attached versus detached |
| 04_controls.py | FR-012 dropped control, the multi-worker unavailable control |
| 16_filter_fixture.py | the content-filter fixture every traffic stage depends on, provisioned by the suite itself |
| 02_history_purge.py | SC-003/004/005, FR-006..FR-010 and the status surface |
| 03_pause_restart.sh | SC-006, FR-011 no_reports, FR-013 pause |
| 05_real_traffic_matrix.sh | SC-008 — the content-filter matrix with collection active |

## Environment

```
panel root   /root/dev/panel
panel url    http://127.0.0.1:8001
restart cmd  bash /root/dev/tl_restart.sh
matrix       /root/dev/panel/specs/002-live-traffic-log/experiments/../../001-content-filtering/experiments/05_real_traffic_matrix.sh
```

## proxies.sh up

```
demo socks proxies listening: 2/2 on 10821 demo-kids and 10822 demo-open, held by 2 process(es)
```

exit status: 0

## 16_filter_fixture.py

```
=== the suite provisions its own content-filter fixture ===
  catalog -> 200                                                 OK
  every category the suite needs exists in the catalog           OK
  profiles -> 200                                                OK
    the profile as the panel holds it now: categories=['adult', 'instagram', 'pglist-42', 'pglist-52', 'tiktok'] allow=['*.khanacademy.org', 'wikipedia.org'] block=['=example.org'] strict=True
  the profile carries every category the suite needs             OK
  the profile is strict                                          OK
  assignments -> 200                                             OK
  apply -> 200                                                   OK
  the assignment reports itself enforced                         OK
  the assignment carries no error                                OK
  www.pornhub.com routes to BLOCK before any traffic runs        OK
  www.wikipedia.org routes to DIRECT before any traffic runs     OK

    fixture changes this run: none, the panel already matched
ALL filter-fixture CHECKS PASSED
```

exit status: 0

## 00_parse.py

```
=== verified lines from the test node (research.md §1) ===
  '->' DIRECT wikipedia: host                                    OK
  '->' DIRECT wikipedia: port                                    OK
  '->' DIRECT wikipedia: protocol                                OK
  '->' DIRECT wikipedia: inbound                                 OK
  '->' DIRECT wikipedia: route                                   OK
  '->' DIRECT wikipedia: refused                                 OK
  '->' DIRECT wikipedia: user_id                                 OK
  '->' DIRECT wikipedia: node_id                                 OK
  '->' DIRECT wikipedia: at                                      OK
  '>>' DIRECT google: host                                       OK
  '>>' DIRECT google: port                                       OK
  '>>' DIRECT google: protocol                                   OK
  '>>' DIRECT google: inbound                                    OK
  '>>' DIRECT google: route                                      OK
  '>>' DIRECT google: refused                                    OK
  '>>' DIRECT google: user_id                                    OK
  '-> BLOCK' pornhub is refused: host                            OK
  '-> BLOCK' pornhub is refused: port                            OK
  '-> BLOCK' pornhub is refused: protocol                        OK
  '-> BLOCK' pornhub is refused: route                           OK
  '-> BLOCK' pornhub is refused: refused                         OK
  '-> BLOCK' pornhub is refused: user_id                         OK

=== shape variants ===
  udp line: host                                                 OK
  udp line: port                                                 OK
  udp line: protocol                                             OK
  udp line: refused                                              OK
  udp line: user_id                                              OK
  IPv6 bracketed host: host                                      OK
  IPv6 bracketed host: port                                      OK
  IPv6 bracketed host: protocol                                  OK
  IPv6 bracketed host: user_id                                   OK
  IPv6 source address does not confuse the parser: host          OK
  IPv6 source address does not confuse the parser: port          OK
  IPv6 source address does not confuse the parser: user_id       OK
  tags with spaces preserved: inbound                            OK
  tags with spaces preserved: route                              OK
  tags with spaces preserved: refused                            OK
  non-numeric email: user_id                                     OK
  non-numeric email: user_label                                  OK
  non-numeric email: host                                        OK
  high port and numeric host: host                               OK
  high port and numeric host: port                               OK
  high port and numeric host: user_id                            OK

=== non-access lines ===
  warning line -> None                                           OK
  info line -> None                                              OK
  empty line -> None                                             OK
  access-like line without email -> None                         OK
  rejected connection line -> None                               OK

=== privacy: the source address never reaches the event ===
    event fields: ['at', 'host', 'inbound', 'node_id', 'port', 'protocol', 'refused', 'route', 'user_id', 'user_label']
  no attribute contains the source address                       OK
  no attribute contains the source IP                            OK
  no attribute contains the source port                          OK

=== refused is always a real boolean, never null (contract guard) ===
  parsed tcp   -> DIRECT                                         OK
    refused is a bool                                            OK
    refused matches the route                                    OK
  parsed tcp   >> DIRECT                                         OK
    refused is a bool                                            OK
    refused matches the route                                    OK
  parsed udp   -> DIRECT                                         OK
    refused is a bool                                            OK
    refused matches the route                                    OK
  parsed udp   >> DIRECT                                         OK
    refused is a bool                                            OK
    refused matches the route                                    OK
  parsed tcp   -> BLOCK                                          OK
    refused is a bool                                            OK
    refused matches the route                                    OK
  parsed tcp   >> BLOCK                                          OK
    refused is a bool                                            OK
    refused matches the route                                    OK
  parsed udp   -> BLOCK                                          OK
    refused is a bool                                            OK
    refused matches the route                                    OK
  parsed udp   >> BLOCK                                          OK
    refused is a bool                                            OK
    refused matches the route                                    OK
  parsed tcp   -> gemini-usa                                     OK
    refused is a bool                                            OK
    refused matches the route                                    OK
  parsed tcp   >> gemini-usa                                     OK
    refused is a bool                                            OK
    refused matches the route                                    OK
  parsed udp   -> gemini-usa                                     OK
    refused is a bool                                            OK
    refused matches the route                                    OK
  parsed udp   >> gemini-usa                                     OK
    refused is a bool                                            OK
    refused matches the route                                    OK
  parsed tcp   -> india_wireguard                                OK
    refused is a bool                                            OK
    refused matches the route                                    OK
  parsed tcp   >> india_wireguard                                OK
    refused is a bool                                            OK
    refused matches the route                                    OK
  parsed udp   -> india_wireguard                                OK
    refused is a bool                                            OK
    refused matches the route                                    OK
  parsed udp   >> india_wireguard                                OK
    refused is a bool                                            OK
    refused matches the route                                    OK
  parsed tcp   -> Some Route With Spaces                         OK
    refused is a bool                                            OK
    refused matches the route                                    OK
  parsed tcp   >> Some Route With Spaces                         OK
    refused is a bool                                            OK
    refused matches the route                                    OK
  parsed udp   -> Some Route With Spaces                         OK
    refused is a bool                                            OK
    refused matches the route                                    OK
  parsed udp   >> Some Route With Spaces                         OK
    refused is a bool                                            OK
    refused matches the route                                    OK
  only bool was ever produced                                    OK

ALL parse CHECKS PASSED
```

exit status: 0

## 11_bucket_key.py

```
=== a disposable copy of the panel database is used ===
  the copy exists                                                OK

=== two events that differ ONLY in the outbound must not merge ===
  the two outbounds produced two buckets                         OK
  every key carries all ten fields                               OK
  the repeated outbound accumulated its hits                     OK

=== the flush must write them, which is what the nine-field unpack broke ===
    rows written: [('DIRECT', 2), ('SECOND', 1)]
  both rows reached the database                                 OK
  each row kept its own outbound and hit count                   OK
  every row carries the destination                              OK

=== expiring an old bucket must compare the bucket start, not the refused flag ===
  the stale event opened its own bucket                          OK
    buckets before the flush 3, after 2
  the stale bucket was expired by the flush                      OK
  no stale bucket survived                                       OK
  the current buckets survived                                   OK

ALL bucket-key CHECKS PASSED
```

exit status: 0

## 01_live_e2e.py

```
=== preconditions ===
  socks proxy for demo-kids (10821) reachable                    OK
  socks proxy for demo-open (10822) reachable                    OK

=== stage 0: collector status (poll up to 60 s) ===
    status after 0.1 s: {"enabled": true, "available": true, "reason": null, "retention_hours": 48, "max_records": 2000000, "ceiling_active": false, "purge_incomplete": false, "last_purge_at": null, "purged_expired": 0, "purged_over_ceiling": 0, "nodes": [{"node_id": 1, "node": "test-singbox-node", "state": "no_reports", "since": "2026-09-17T01:31:23.505199Z", "lines": 5, "events": 0, "dropped": 0, "dropped_records": 0, 
  status: available true                                         OK
  status: node 5 state collecting                                OK

=== stage 1: warm the identity cache (one connection per user, then a flush) ===

=== stage 2: live feed + raw viewer parity ===
  live stream opened (HTTP 200)                                  OK
  raw node log stream opened (HTTP 200)                          OK
    demo-kids  www.wikipedia.org  event after 0.03 s  route=DIRECT refused=False
    demo-kids  www.google.com     event after 0.04 s  route=DIRECT refused=False
    demo-kids  www.pornhub.com    event after 0.04 s  route=BLOCK refused=True
    demo-open  www.wikipedia.org  event after 0.05 s  route=DIRECT refused=False
    demo-open  www.pornhub.com    event after 0.04 s  route=DIRECT refused=False
  demo-kids www.wikipedia.org event within 3 s (0.03 s)          OK
  demo-kids www.google.com event within 3 s (0.04 s)             OK
  demo-kids www.pornhub.com event within 3 s (0.04 s)            OK
  demo-open www.wikipedia.org event within 3 s (0.05 s)          OK
  demo-open www.pornhub.com event within 3 s (0.04 s)            OK
  demo-kids pornhub refused true                                 OK
  demo-kids pornhub route BLOCK                                  OK
  demo-kids wikipedia refused false                              OK
  demo-open pornhub refused false (unrestricted config)          OK
    5 events, 0 control messages in this stage: []
  at least one demo-open event                                   OK
  every demo-open event carries username demo-open               OK
  every demo-kids event carries username demo-kids               OK
  every event has all contract fields                            OK
  every event names node 5 as filter-test-xray                   OK
  every demo event port is 443                                   OK
  every demo event protocol is tcp                               OK
  no event carries a source-address field                        OK
    source addresses the raw node log actually carried: ['203.0.113.10', '[203.0.113.10]']
  the raw log did carry at least one source address to test against OK
  no event value contains any observed client source address     OK
    raw viewer received 5 data lines, 5 access lines
  raw viewer still shows accepted tcp:www.google.com             OK
  raw viewer still shows accepted tcp:www.pornhub.com            OK
  raw viewer still shows accepted tcp:www.wikipedia.org          OK
  raw viewer stream reported no error                            OK
  live stream reported no error                                  OK

=== stage 3: exact-username filter (username=demo-open) ===
  filtered live stream opened (HTTP 200)                         OK
    filtered feed delivered 2 events: [('demo-open', 'www.wikipedia.org'), ('demo-open', 'www.google.com')]
  filtered feed: at least one event arrived                      OK
  filtered feed: every event is demo-open                        OK
  filtered feed: no demo-kids event                              OK

ALL live-e2e CHECKS PASSED
```

exit status: 0

## 05_viewer_parity.py

```
=== preconditions ===
  socks proxy for demo-kids (10821) reachable                    OK
  GET /api/traffic-log/status -> 200                             OK
  collector available                                            OK
  collection enabled                                             OK
  node 5 is collecting                                           OK

=== round 1: the per-node log viewer while the collector is ATTACHED ===
  attached: /api/node/5/logs opened (HTTP 200)                   OK
    attached: 6 data lines, 6 access lines, per-probe {'www.wikipedia.org': 2, 'www.google.com': 2, 'sub.khanacademy.org': 2}
    attached: first access line -> 2026/09/17 01:32:54.522859 from 203.0.113.10:15172 accepted tcp:www.wikipedia.org:443 [Shadowsocks TCP -> DIRECT] email: 72
  attached: the viewer reported no error                         OK
  attached: every accepted line matches the documented shape     OK
  attached: the viewer shows www.wikipedia.org                   OK
  attached: the viewer shows www.google.com                      OK
  attached: the viewer shows sub.khanacademy.org                 OK

=== detaching the collector (PUT /settings enabled=false) ===
  PUT /settings {"enabled":false} -> 200                         OK
  status reports enabled false                                   OK
  node 5 is no longer collecting                                 OK

=== round 2: the same viewer while the collector is DETACHED ===
  detached: /api/node/5/logs opened (HTTP 200)                   OK
    detached: 6 data lines, 6 access lines, per-probe {'www.wikipedia.org': 2, 'www.google.com': 2, 'sub.khanacademy.org': 2}
    detached: first access line -> 2026/09/17 01:33:03.383693 from 203.0.113.10:19000 accepted tcp:www.wikipedia.org:443 [Shadowsocks TCP -> DIRECT] email: 72
  detached: the viewer reported no error                         OK
  detached: every accepted line matches the documented shape     OK
  detached: the viewer shows www.wikipedia.org                   OK
  detached: the viewer shows www.google.com                      OK
  detached: the viewer shows sub.khanacademy.org                 OK

=== re-attaching the collector ===
  PUT /settings {"enabled":true} -> 200                          OK
  status reports enabled true                                    OK
    node 5 reached state collecting after 0 s
  node 5 is collecting again                                     OK

=== SC-008: the per-node viewer attached versus detached ===
    attached: {"lines": 6, "access": 6, "per_host": {"www.wikipedia.org": 2, "www.google.com": 2, "sub.khanacademy.org": 2}, "samples": ["2026/09/17 01:32:54.522859 from 203.0.113.10:15172 accepted tcp:www.wikipedia.org:443 [Shadowsocks TCP -> DIRECT] email: 72", "2026/09/17 01:32:54.644730 from 203.0.113.10:15178 accepted tcp:www.google.com:443 [Shadowsocks TCP >> DIRECT] email: 72"]}
    detached: {"lines": 6, "access": 6, "per_host": {"www.wikipedia.org": 2, "www.google.com": 2, "sub.khanacademy.org": 2}, "samples": ["2026/09/17 01:33:03.383693 from 203.0.113.10:19000 accepted tcp:www.wikipedia.org:443 [Shadowsocks TCP -> DIRECT] email: 72", "2026/09/17 01:33:03.507099 from 203.0.113.10:19014 accepted tcp:www.google.com:443 [Shadowsocks TCP >> DIRECT] email: 72"]}
    per-probe difference: {'www.wikipedia.org': 0, 'www.google.com': 0, 'sub.khanacademy.org': 0}
  attached round captured every probe (6)                        OK
  detached round captured every probe (6)                        OK
  every probe was shown at least 2 times while attached          OK
  every probe was shown at least 2 times while detached          OK
  the two rounds differ by at most one access line per probe     OK

ALL viewer-parity CHECKS PASSED
```

exit status: 0

## 04_controls.py

```
=== stage 1 skipped: the subscriber-overflow flood needs more load than this box can generate ===
    06_drop_paths.py proves the same requirement (FR-012) deterministically, in process.
    set TL_RUN_FLOOD=1 to attempt it anyway.
WARNING:  2026-09-17 01:33:14,931 - [34mTraffic-log[0m - traffic log collector is unavailable: the panel runs more than one web worker, so no single process owns the node log streams

=== stage 2: every unsupported deployment shape refuses to collect ===
  server_settings.workers restored                               OK
  a collector built with workers=2 is unavailable                OK
    reason given: 'the panel runs more than one web worker, so no single process owns the node log streams'
  ...and the reason is a readable sentence, not a bare label     OK
  ...and it names the web workers as the cause                   OK
  role backend restored                                          OK
    role backend   -> available=False reason='this process does not run nodes, so it cannot read their log streams'
  a backend-role collector refuses to collect                    OK
  ...and says why in a sentence                                  OK
  role node restored                                             OK
    role node      -> available=False reason='the panel is split across roles, and traffic log collection needs the process that serves the panel to be the one that owns the node log streams'
  a node-role collector refuses to collect                       OK
  ...and says why in a sentence                                  OK
  role scheduler restored                                        OK
    role scheduler -> available=False reason='this process does not run nodes, so it cannot read their log streams'
  a scheduler-role collector refuses to collect                  OK
  ...and says why in a sentence                                  OK
    status(): {"enabled": true, "available": false, "reason": "the panel runs more than one web worker, so no single process owns the node log streams", "nodes": []}
  status() reports available false                               OK
  status() repeats the same reason                               OK
  a subscriber is told the feed is unavailable, with the reason  OK
  start() attaches nothing while unavailable                     OK
  ensure_attached() attaches nothing while unavailable           OK

ALL control-message CHECKS PASSED
```

exit status: 0

## 12_owner_only.py

```
=== building a role that holds every ordinary permission but is not the owner ===
    resources granted: admin_roles, admins, api_keys, client_templates, cores, groups, hosts, hwids, nodes, settings, system, templates, users
  the role grants nodes.stats                                        OK
  the role grants nodes.logs                                         OK
  the role grants settings.read                                      OK
  the role grants settings.update                                    OK
  POST /api/admin-role -> 200/201                                    OK
  the created role is not an owner role                              OK
  POST /api/admin -> 200/201                                         OK
  the maximal admin can log in                                       OK

=== every restricted endpoint must refuse an admin that is not the panel owner ===
  GET  /api/traffic-log/status                                    -> 403 OK
  GET  /api/traffic-log/history                                   -> 403 OK
  GET  /api/traffic-log/summary                                   -> 403 OK
  GET  /api/traffic-log/live                                      -> 403 OK
  PUT  /api/traffic-log/settings                                  -> 403 OK
  POST /api/traffic-log/purge                                     -> 403 OK
  GET  /api/content-filter/catalog                                -> 403 OK
  GET  /api/content-filter/targets                                -> 403 OK
  GET  /api/content-filter/profiles                               -> 403 OK
  GET  /api/content-filter/assignments                            -> 403 OK
  POST /api/content-filter/test                                   -> 403 OK
  GET  /api/node/inbounds/usage                                   -> 403 OK
  GET  /api/nodes/realtime_stats                                  -> 403 OK

=== the same endpoints must still answer the panel owner ===
  GET  /api/traffic-log/status                                    -> 200 OK
  GET  /api/traffic-log/history                                   -> 200 OK
  GET  /api/traffic-log/summary                                   -> 200 OK
  GET  /api/content-filter/catalog                                -> 200 OK
  GET  /api/content-filter/targets                                -> 200 OK
  GET  /api/content-filter/profiles                               -> 200 OK
  GET  /api/content-filter/assignments                            -> 200 OK
  GET  /api/node/inbounds/usage                                   -> 200 OK
  GET  /api/nodes/realtime_stats                                  -> 200 OK

=== a route the maximal admin may still use, so the role is proven functional ===
  GET  /api/nodes/simple                                     -> 200  OK

=== cleanup ===
  the temporary admin was deleted                                    OK
  the temporary role was deleted                                     OK
  the temporary admin can no longer log in                           OK

ALL owner-only CHECKS PASSED
```

exit status: 0

## 13_cleanup_faults.py

```
=== fault 1: the creation response is lost after the server already committed it ===
  the role really was created on the panel                           OK
  the admin really was created on the panel                          OK
    the ids stay unrecorded, which is exactly what a lost response leaves behind
  no role id was recorded                                            OK
  no admin id was recorded                                           OK
  cleanup removed the admin it never saw a response for              OK
  cleanup removed the role it never saw a response for               OK
  the admin no longer exists on the panel                            OK
  the role no longer exists on the panel                             OK

=== fault 2: deleting the admin keeps failing, the role must still be attempted ===
  the role was created for the second fault                          OK
  the admin was created for the second fault                         OK
    deleting the temporary admin failed: URLError('simulated failure deleting the admin')
    deleting the temporary admin failed: URLError('simulated failure deleting the admin')
    deleting the temporary admin failed: URLError('simulated failure deleting the admin')
    LEFTOVER: admin tl-maximal-0ic9as could not be deleted
    LEFTOVER: role tl-maximal-role-0ic9as (id 4) could not be confirmed deleted
  cleanup reported the admin as NOT removed                          OK
  cleanup still attempted the role deletion                          OK
  cleanup retried the admin deletion                                 OK
  a clean retry removes the admin                                    OK
  nothing is left behind                                             OK

ALL cleanup-fault CHECKS PASSED
```

exit status: 0

## 14_single_reader.py

```
=== the collector is detached, so the viewers must share one node stream ===
  no collector reader is attached for this node                    OK
  exactly one node stream was opened for two viewers               OK
  and only one was ever open at a time                             OK
    first viewer received 6 lines, second received 6
  the first viewer saw every line                                  OK
  the second viewer saw every line too                             OK
  neither viewer lost lines to the other                           OK
  the shared stream closed once the last viewer left               OK
  no direct pump is left running                                   OK
  no direct reader registration is left behind                     OK

ALL single-reader CHECKS PASSED
```

exit status: 0

## 15_purge_race.py

```
=== a full purge must not discard events that arrived while it ran ===
    buckets kept: ['fully.flushed.example', 'newer.example', 'straddling.example']
  a bucket entirely older than the purge is dropped                      OK
  a bucket that straddles the purge is KEPT                              OK
  a bucket created after the purge is kept                               OK
  a bucket flushed AFTER the purge began is kept, because the purge deleted its row OK
  the straddling bucket keeps every hit, because none of them are in the database now OK
  and so does one whose every hit had been written                       OK
  its first_seen is moved up to the purge moment                         OK
  its last_seen is untouched                                             OK
  every survivor starts unwritten                                        OK
  every survivor has nothing counted as written                          OK
  the row watermark was reset                                            OK
  the ceiling floor was reset                                            OK
  the identity watch list was reset                                      OK

=== a straddling bucket carries its unwritten pre-purge hits across, by design ===
  every hit survives the purge                                           OK
  and they are all re-dated to the purge moment                          OK

=== an age-based purge must not take events that arrived after its cutoff ===
  a bucket whose last event predates the cutoff is dropped               OK
  a bucket still receiving events after the cutoff is KEPT               OK
  it keeps every hit, because the purge deleted its row too              OK
  it is re-dated to the cutoff                                           OK
  and it counts as unwritten again                                       OK
  an age-based purge leaves the row watermark alone                      OK
  and leaves the ceiling floor alone                                     OK

=== survival is decided by ingestion order, so a clock that steps backwards cannot lose an event ===
  a bucket last touched before the request is dropped                    OK
  a bucket touched after it is KEPT even though its clock reads earlier  OK
  and no carried bucket ever claims to have started after it ended       OK

=== an incomplete delete must never orphan or duplicate a stored row ===
  a bucket whose row really survived keeps pointing at it                OK
  and keeps what it already wrote, so nothing is written twice           OK
  a bucket whose row was deleted forgets it, instead of updating nothing OK
  and counts every hit as unwritten again                                OK
  so none of its traffic is stranded                                     OK

=== an age purge that empties the table must reset the row watermark ===
  the row watermark was reset                                            OK
  the ceiling floor was reset                                            OK
  the identity watch list was reset                                      OK

=== but a partial age purge must leave the watermark alone ===
  the row watermark survives                                             OK
  the ceiling floor survives                                             OK

=== a slow viewer's dropped lines are counted, not silently discarded ===
  two dropped lines were counted                                         OK
  a drop for an unknown node left the known counter alone                OK
  a drop for an unknown node created no state for it                     OK

ALL purge-race CHECKS PASSED
```

exit status: 0

## 02_history_purge.py

```
INFO:     2026-09-17 01:41:20,914 - [34mJobs[0m - Traffic log purge removed 120 records older than 48 hours, 51 records over the 50 record ceiling, reconciled 0 identities and forgot 2 unreferenced ones
=== part 3: retention + ceiling, in-process, TRAFFIC_LOG_MAX_RECORDS=50 ===
    !! this part DELETES every record above the ceiling, so it never touches the panel's own database
    !! it runs against a throwaway copy: sqlite+aiosqlite:////tmp/traffic_log_ceiling_copy.sqlite3
  the ceiling pass was handed a disposable database              OK
  job_settings picked up the ceiling from the environment        OK
  seeded 200 rows                                                OK
    rows in the copy before the purge: 221 (max seeded id 221)
  no row older than 48 h remains                                 OK
  all 3-day-old seeded rows are gone                             OK
  total rows <= ceiling (50)                                     OK
  at least one recent seeded row survived                        OK
  surviving seeded rows are the highest seeded ids               OK
    rows after the purge: 50, surviving seeded: 50
    status payload the operator would receive: {"enabled": true, "available": true, "reason": null, "retention_hours": 48, "max_records": 50, "ceiling_active": true, "purge_incomplete": false, "last_purge_at": "2026-09-17 01:41:20.914367+00:00", "purged_expired": 120, "purged_over_ceiling": 51}
  status payload ceiling_active is True                          OK
  status payload retention_hours is 48                           OK
  status payload max_records is the configured ceiling           OK
  status payload purged_expired >= 120                           OK
  status payload purged_over_ceiling >= 1                        OK
  status payload last_purge_at is set                            OK
  ceiling reset to the default for the normal run                OK
  normal run deletes nothing recent                              OK
    status payload after the normal run: ceiling_active=False
  the purge job is registered as traffic_log_purge               OK
    traffic_log_purge interval: 600.0 s (TRAFFIC_LOG_PURGE_INTERVAL=600)
  SC-004: the purge job runs at least every 900 s                OK
  SC-004: the configured purge interval cannot exceed 900 s      OK

ALL ceiling CHECKS PASSED
=== part 4: history latency with the store at its ceiling (SC-003) ===
    !! this part seeds 2000000 rows into the panel's OWN database to reach the 2000000 record ceiling
    !! while they are in place max(id) - ceiling turns positive, so the panel's purge starts
    !! evicting the oldest records — exactly the FR-010 behaviour this part proves
    !! the seeded rows are deleted again at the end; run this part LAST
    seeded 2000000 rows in 192.3 s (21 rows were already there)
  load rows present                                              OK
    SC-003 measured with 2000021 rows in the store against a ceiling of 2000000
  SC-003 was measured with the store at or above its ceiling     OK
  history first page -> 200                                      OK
  history first page under 2 s (0.01 s)                          OK
  history first page holds 100 items                             OK
  history first page for demo-kids under 2 s (0.01 s)            OK
    summary over the load took 17.13 s (HTTP 200)
    waiting for the panel's own purge to report the ceiling over HTTP (up to 700 s)
    status after 380 s: {"enabled": true, "available": true, "reason": null, "retention_hours": 48, "max_records": 2000000, "ceiling_active": true, "purge_incomplete": false, "last_purge_at": "2026-09-17T01:51:12.047342Z", "purged_expired": 0, "purged_over_ceiling": 21}
  FR-010: GET /status reports ceiling_active true at the ceiling OK
  FR-010: GET /status reports purged_over_ceiling >= 1           OK
  FR-010: GET /status still reports max_records                  OK
  FR-010: GET /status carries last_purge_at                      OK
    deleted 2000000 load rows in 149.1 s
  no load rows left behind                                       OK

ALL load CHECKS PASSED
=== part 1: history and summary over HTTP ===
  history last hour for demo-kids -> 200                         OK
    11 items: [('www.google.com', 5, False), ('www.pornhub.com', 2, True), ('www.wikipedia.org', 7, False), ('sub.khanacademy.org', 2, False), ('example.org', 1, True), ('dns.google', 1, True), ('www.instagram.com', 1, True), ('www.pornhub.com', 1, True), ('sub.khanacademy.org', 1, False), ('www.wikipedia.org', 1, False)]
  history contains at least one item                             OK
  history lists www.pornhub.com                                  OK
  this run's own www.pornhub.com rows are refused                OK
  every item has hits >= 1                                       OK
  every item has first_seen <= last_seen                         OK
  every item belongs to demo-kids                                OK
  every item is inside the requested range                       OK
  items ordered by last_seen desc, id desc                       OK
  every item names node filter-test-xray                         OK
  items carry user_deleted false                                 OK
  start=now-3d -> 422                                            OK
  422 detail mentions the 48 hours limit                         OK
  end <= start -> 422                                            OK
  page 1 (limit=2) -> 200                                        OK
  page 1 has 2 items                                             OK
  page 1 carries next_cursor                                     OK
  page 2 via next_cursor -> 200                                  OK
  page 2 has at least one item                                   OK
  no duplicate ids across pages                                  OK
  page 2 continues strictly after page 1                         OK
  pages together equal the unpaged prefix                        OK
  summary for demo-kids -> 200                                   OK
    summary: {"connections": 23, "destinations": 7, "users": 1, "refused": 6, "top_users": [{"user_id": 72, "username": "demo-kids", "hits": 23}], "top_destinations": [{"host": "www.wikipedia.org", "hits": 8, "refused": 0}, {"host": "www.google.com", "hits": 6, "refused": 0}, {"host": "sub.khanacademy.org", "hits": 3, "refused": 0}, {"host": "www.pornhub.com", "hits": 3, "refused": 3}, {"host": "dns.google", "
  summary refused >= 1                                           OK
  summary connections >= refused                                 OK
  summary users == 1                                             OK
  summary top_destinations contains www.pornhub.com              OK
  summary top_users is demo-kids only                            OK
  summary top lists are capped at 8                              OK

=== part 2: the operator's status surface over HTTP (FR-010/011/012) ===
  GET /status -> 200                                             OK
    {"enabled": true, "available": true, "reason": null, "retention_hours": 48, "max_records": 2000000, "ceiling_active": false, "purge_incomplete": false, "last_purge_at": null, "purged_expired": 0, "purged_over_ceiling": 0}
  status: available true                                         OK
  status: enabled true                                           OK
  status: retention_hours is 48                                  OK
  status: max_records is the configured ceiling                  OK
  status: ceiling_active is a boolean                            OK
  status: purged_expired is an integer                           OK
  status: purged_over_ceiling is an integer                      OK
    nodes: [(1, 'no_reports', 5, 0, 0), (3, 'no_reports', 3, 0, 0), (5, 'collecting', 29, 22, 0)]
  status: at least one node is listed                            OK
  status: every node state is a documented state                 OK
  status: every node carries the contract fields                 OK
  status: every node reports a dropped counter                   OK
    waiting for the scheduled purge to report itself (0 s so far)
    waiting for the scheduled purge to report itself (20 s so far)
    waiting for the scheduled purge to report itself (40 s so far)
    waiting for the scheduled purge to report itself (60 s so far)
    waiting for the scheduled purge to report itself (80 s so far)
    waiting for the scheduled purge to report itself (100 s so far)
    waiting for the scheduled purge to report itself (120 s so far)
    waiting for the scheduled purge to report itself (140 s so far)
    waiting for the scheduled purge to report itself (160 s so far)
    waiting for the scheduled purge to report itself (180 s so far)
    waiting for the scheduled purge to report itself (200 s so far)
    waiting for the scheduled purge to report itself (220 s so far)
    waiting for the scheduled purge to report itself (240 s so far)
    waiting for the scheduled purge to report itself (260 s so far)
    waiting for the scheduled purge to report itself (280 s so far)
    waiting for the scheduled purge to report itself (300 s so far)
    waiting for the scheduled purge to report itself (320 s so far)
    waiting for the scheduled purge to report itself (340 s so far)
    waiting for the scheduled purge to report itself (360 s so far)
    waiting for the scheduled purge to report itself (380 s so far)
    waiting for the scheduled purge to report itself (400 s so far)
    last_purge_at: '2026-09-17T01:41:11.989877Z' after 420 s
  status: the scheduled purge reported last_purge_at within 700 s OK

=== preparing a disposable database for the ceiling pass ===
    copied /root/dev/panel/db.sqlite3 -> /tmp/traffic_log_ceiling_copy.sqlite3 (0.6 MB)
  a disposable database is available for the ceiling pass        OK

  the ceiling pass finished without failures                     OK
    removed the disposable copy

  the load pass finished without failures                        OK

ALL history-purge CHECKS PASSED
```

exit status: 0

## 03_pause_restart.sh

```
=== preconditions ===
  GET /api/traffic-log/status -> 200                             OK
    node 5 state before the run: no_reports (enabled=true)
  collector available                                            OK

=== stage 1: pause collection ===
  PUT /settings {"enabled":false} -> 200                         OK
  paused status reports enabled false                            OK
  every node state is paused                                     OK

=== stage 2: the live feed announces the pause (3s) ===
    live line: data: {"control": "paused"}
  live feed emits a paused control message                       OK

=== stage 3: the raw node log viewer keeps working while paused (8s) ===
    raw viewer produced 2 lines, 1 of them access lines
    probe line: data: 2026/09/17 01:53:55.076284 from 203.0.113.10:31086 accepted tcp:www.wikipedia.org:443 [Shadowsocks TCP -> DIRECT] email: 72
  raw viewer still streams accepted tcp: lines while paused      OK
  raw viewer shows the connection made through 10821             OK

=== stage 4: resume collection ===
  PUT /settings {"enabled":true} -> 200                          OK
  resumed status reports enabled true                            OK
    node 5 reached state collecting after 0s
  node 5 collecting within 90s (0s)                              OK

=== stage 5: panel restart ===
    running: bash /root/dev/tl_restart.sh
    panel up after 8s (log /root/dev/panel-restart-20260917-015402.log)
  restart command exited 0                                       OK
    node 5 state collecting, 11s of polling, 19s since the restart began
  node 5 collecting again after the restart                      OK
  collection resumed within 60s (19s)                            OK

=== stage 6: a node that reports no destinations shows no_reports (FR-011) ===
    node 5 after the restart: state=collecting lines=13 events=6
  node 5 has seen at least one log line                          OK
    leaving the node idle for up to 120s (no traffic is driven through it)
    node 5 state no_reports after 60s of idling
  idle node reports no_reports within 120s (60s)                 OK

=== stage 7: one destination report puts the node back to collecting ===
    node 5 state collecting, events=7
  node 5 collecting again within 90s (1s)                        OK

ALL pause-restart CHECKS PASSED
```

exit status: 0

## 06_drop_paths.py

```
=== a live subscriber that stops reading is told lines were dropped (FR-012) ===
  the subscriber queue is capped                                 OK
  the node counted live drops                                    OK
  a dropped control message reached the subscriber               OK
  the dropped control carries a positive count                   OK
    live drops counted: 51, control messages: 1

=== a log viewer that stops reading loses lines, and they are counted ===
  the viewer queue is capped                                     OK
  the node counted viewer drops                                  OK
    viewer drops counted: 26

=== the bucket map refuses to grow past its cap and counts what it refused ===
  bucket map stayed at its cap                                   OK
  the node counted record drops                                  OK
  the aggregate dropped counter reflects them                    OK
    buckets held: 50000, record drops: 40

=== a subscriber scoped to one admin never receives an unresolved owner ===
  no event leaked to the foreign admin                           OK
  the scoped subscriber received nothing at all                  OK

=== an unscoped subscriber DOES receive the same events (the filter is the only reason) ===
  the unscoped subscriber received every event                   OK
  each event names the destination                               OK

=== counters survive a status snapshot round trip ===
  snapshot carries dropped                                       OK
  snapshot carries dropped_live                                  OK
  snapshot carries dropped_viewer                                OK
  snapshot carries dropped_records                               OK
  snapshot carries stream_full                                   OK
  snapshot carries restreams                                     OK
  snapshot carries records                                       OK

ALL drop-path CHECKS PASSED
```

exit status: 0

## 07_retention_and_purge.py

```
=== the migration gave the pre-existing state row a real default ===
  traffic_log_state has retention_hours                            OK
    state rows: [(1, 1, 48)]
  exactly one state row                                            OK
  its retention is a usable number                                 OK

=== status reports the live retention ===
  GET /status -> 200                                               OK
    retention now: 48

=== retention is settable and takes effect immediately ===
  PUT retention_hours=6 -> 200                                     OK
  status echoes the new retention                                  OK
  a fresh status still reports 6                                   OK

=== the history window follows the configured retention, at the boundary ===
  a start 5h30m back is accepted while retention is 6h             OK
  a start 7h back is refused while retention is 6h                 OK
  the refusal names the configured window                          OK
    refusal text: history is kept for 6 hours; choose a start within that window

=== bounds are enforced ===
  retention 0 is rejected                                          OK
  retention 721 is rejected                                        OK
  an empty settings body is rejected                               OK

=== the automatic purge uses the configured window ===
  seeded rows are present                                          OK
INFO:     2026-09-17 01:55:42,816 - [34mJobs[0m - Traffic log purge removed 40 records older than 6 hours, 0 records over the 2000000 record ceiling, reconciled 0 identities and forgot 1 unreferenced ones
  rows older than the 6h window were purged                        OK
  rows inside the window survived                                  OK

=== on-demand purge: older-than ===
  POST /purge older_than_hours=6 -> 200                            OK
  it removed the aged rows                                         OK
  it kept the fresh rows                                           OK
  the result reports a positive removal count                      OK
    purge result: {"removed": 30, "incomplete": false, "remaining": 72, "retention_hours": 6, "reclaimed": false, "freed_bytes": null}

=== on-demand purge: everything ===
  POST /purge with no body -> 200                                  OK
  nothing remains afterwards                                       OK
  the database agrees the table is empty                           OK

=== a flush cannot resurrect what was purged ===
  no purged synthetic row came back                                OK
  retention restored to its original value                         OK

ALL retention/purge CHECKS PASSED
```

exit status: 0

## 08_retention_across_processes.py

```
=== the API sets retention in the PANEL process ===
  the panel now reports 3 hours                                    OK

=== a SEPARATE process whose collector never started must still honour it ===
INFO:     2026-09-17 01:56:01,475 - [34mJobs[0m - Traffic log purge removed 25 records older than 3 hours, 0 records over the 2000000 record ceiling, reconciled 0 identities and forgot 0 unreferenced ones
  this process's collector never started                           OK
  its cached retention is the bare default                         OK
  effective_retention_hours() reads the database instead           OK
  seeded both ages                                                 OK
  rows older than the API-set 3h window were purged                OK
  rows inside that window survived                                 OK

=== once the collector HAS loaded state, the cached value is used ===
  a started collector trusts its own cache                         OK
  an unstarted one re-reads and sees 12                            OK

=== and the running panel itself already knows about the change ===
  the panel process reports 12                                     OK
  retention restored                                               OK

ALL multi-process retention CHECKS PASSED
```

exit status: 0

## 09_disk_reclamation.py

```
=== seeding 250000 rows so there is real space to reclaim ===
    database plus wal: 771.2 MB
  the database really grew                                         OK

=== one purge call is capped, and says so honestly ===
    first call: {"removed": 200000, "remaining": 50000, "incomplete": true, "reclaimed": false, "freed_bytes": null}
  the first call removed a large batch                             OK
  it reports incomplete while rows remain                          OK
  it honestly reports no reclamation                               OK

=== repeating until done, exactly as the dashboard button does ===
    finished after 2 call(s): {"removed": 50000, "remaining": 0, "incomplete": false}
  every record is gone                                             OK
  the last call is not marked incomplete                           OK
  but the files did NOT shrink                                     OK
    still on disk: 771.2 MB
    re-seeded 60000 rows so the reclaim stage has something to free

=== now reclaim, with readers and a writer hammering the database ===
    result: {"removed": 60004, "remaining": 0, "reclaimed": true, "freed_bytes": 808016792}
    took 0.6s, files now 0.6 MB
  the endpoint answered                                            OK
  it reported freed bytes                                          OK
  the files really shrank                                          OK

=== the panel stayed usable throughout ===
  no reader saw an error                                           OK
    writer errors (a busy database is acceptable, a corrupt one is not): 0
  no writer error mentions corruption                              OK
  status still answers                                             OK

ALL reclamation CHECKS PASSED
```

exit status: 0

## 10_restart_recovery.py (restarts the core)

```
  node is collecting before the test                             OK
  baseline traffic was captured                                  OK
   baseline: lines=16 events=9 restreams=0

=== restarting core 1 underneath the collector (no panel restart) ===
   core restarted at 1789610210
   right after: lines=23 events=9 restreams=1 state=collecting

=== driving traffic every 10 s until the collector picks it up again ===
   + 38s lines=25    events=11    restreams=1   state=collecting
  collection recovered WITHOUT a panel restart                   OK
   recovered 38 s after the core restart
  recovery took under 120 s                                      OK
  the reader reopened its stream at least once                   OK

ALL restart-recovery CHECKS PASSED
```

exit status: 0

## proxies.sh down

```
demo socks proxies still listening: 0, processes still holding the ports: 0
```

exit status: 0

## 001/05_real_traffic_matrix.sh

```
  site                                               verdict
  www.google.com           restricted=200  open=200   OK   allowed (200)
  www.wikipedia.org        restricted=200  open=200   OK   allowed (200)
  sub.khanacademy.org      restricted=200  open=200   OK   allowed (200)
  www.pornhub.com          restricted=000  open=200   OK   filtered (open=200 proves the tunnel works)
  www.instagram.com        restricted=000  open=200   OK   filtered (open=200 proves the tunnel works)
  dns.google               restricted=000  open=200   OK   filtered (open=200 proves the tunnel works)
  example.org              restricted=000  open=200   OK   filtered (open=200 proves the tunnel works)
  stray test sockets: 3

TRAFFIC MATRIX: ALL PASSED
```

exit status: 0

## Summary

| step | exit status |
|---|---|
| proxies.sh up | 0 |
| 16_filter_fixture.py | 0 |
| 00_parse.py | 0 |
| 11_bucket_key.py | 0 |
| 01_live_e2e.py | 0 |
| 05_viewer_parity.py | 0 |
| 04_controls.py | 0 |
| 12_owner_only.py | 0 |
| 13_cleanup_faults.py | 0 |
| 14_single_reader.py | 0 |
| 15_purge_race.py | 0 |
| 02_history_purge.py | 0 |
| 03_pause_restart.sh | 0 |
| 06_drop_paths.py | 0 |
| 07_retention_and_purge.py | 0 |
| 08_retention_across_processes.py | 0 |
| 09_disk_reclamation.py | 0 |
| 10_restart_recovery.py (restarts the core) | 0 |
| proxies.sh down | 0 |
| 001/05_real_traffic_matrix.sh | 0 |
