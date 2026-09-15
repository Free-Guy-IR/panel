# Raw experiment output — 2026-09-15

Captured verbatim from the test panel. Every claim in `../research.md` traces to a block below.

## Environment

```
panel   /root/dev/panel (test panel), admin testadmin, owner role
node    filter-test-xray, panel node id 5, ports 62950/62951, core_config_id 1
core 1  type=xray
        inbounds:  [('Shadowsocks TCP', 'shadowsocks', 1080)]
        outbounds: [('DIRECT', 'freedom'), ('BLOCK', 'blackhole')]
        routing rules before the experiment: 1
          {"ip": ["geoip:private"], "outboundTag": "BLOCK", "type": "field"}
node version 0.6.11, xray 26.3.27
```

## 00 — the routing service is not published by default

First run of `01_live_apply.py`, before any core change:

```
=== 0. what backends are running ===
  list_backends                      ERR backends

=== 1. baseline routing rules ===
    ERR NodeAPIError(code=501, detail=unknown service xray.app.router.command.RoutingService)
    count: 1

=== 2. baseline TestRoute (nothing added yet) ===
  blocked-domain candidate           ERR NodeAPIError(code=501, detail=unknown service xray.app.router.command.RoutingService)
  neutral domain                     ERR NodeAPIError(code=501, detail=unknown service xray.app.router.command.RoutingService)

=== 3. APPEND a block rule (should_reset=False) ===
  add_routing_rule                   ERR NodeAPIError(code=501, detail=unknown service xray.app.router.command.RoutingService)
```

`00_enable_routing_service.py`, including the two failed attempts that establish `restart_nodes` is a **query** parameter and not a body field:

```
GET core 1 -> 200
current api section: None
PUT core 1 -> 422
  {"detail":{"restart_nodes":"Field required"}}        <- restart_nodes in the body
...
PUT core 1 -> 422
  {"detail":{"restart_nodes":"Field required"}}        <- still in the body
...
GET core 1 -> 200
current api section: None
PUT core 1 -> 200                                      <- ?restart_nodes=true in the URL
api section now: {'services': ['RoutingService']}
```

Node afterwards:

```
id=5 name=filter-test-xray status=connected msg= xray=26.3.27 ver=0.6.11
```

## 01 — live apply, with no restart

Second run of `01_live_apply.py`, after the core change:

```
=== 1. baseline routing rules ===
    rule tag=<untagged>             -> API
    rule tag=PG_NODE_MALFORMED_DOMAIN_GUARD -> BLOCK
    rule tag=<untagged>             -> BLOCK
    count: 3

=== 2. baseline TestRoute (nothing added yet) ===
  blocked-domain candidate           ERR NodeAPIError(code=500, detail=common: not enough information for making a decision)
  neutral domain                     ERR NodeAPIError(code=500, detail=common: not enough information for making a decision)

=== 3. APPEND a block rule (should_reset=False) ===
  add_routing_rule                   OK

=== 4. rules after append — WHERE did it land? ===
    [0] tag=<untagged>             -> API
    [1] tag=PG_NODE_MALFORMED_DOMAIN_GUARD -> BLOCK
    [2] tag=<untagged>             -> BLOCK
    [3] tag=flt-test-1             -> BLOCK

=== 5. did it actually take effect, with NO restart? ===
  blocked domain now routes to       BLOCK
  neutral domain still routes to     ERR NodeAPIError(code=500, detail=common: not enough information for making a decision)

=== 6. remove it by tag ===
  remove_routing_rule                OK
  rule count after removal           3
  blocked domain routes to           ERR NodeAPIError(code=500, detail=common: not enough information for making a decision)
```

Reading: index `[3]` is the append position. `BLOCK` at step 5 versus unmatched at step 2 is the whole result — the rule changed the router's decision with no restart. `not enough information for making a decision` is the *unmatched* signal, not a failure.

## 02 — ordering, duplicate tags, removal of a missing tag

`02_ordering_and_tags.py`:

```
=== A. ORDERING: does an appended rule lose to an earlier one? ===
    order now: ['<untagged>', 'PG_NODE_MALFORMED_DOMAIN_GUARD', '<untagged>', 'flt-allow-first', 'flt-block-second']
    example.com -> DIRECT   (DIRECT proves first-match-wins, BLOCK would disprove it)

=== B. remove the earlier one, does the later now win? ===
    order now: ['<untagged>', 'PG_NODE_MALFORMED_DOMAIN_GUARD', '<untagged>', 'flt-block-second']
    example.com -> BLOCK

=== C. re-adding a tag that already exists ===
    duplicate rejected: NodeAPIError(code=500, detail=app/router: duplicate ruleTag flt-block-second)

=== D. removing a tag that does not exist ===
    silently accepted (no error)

=== E. state before restart ===
    tags: ['<untagged>', 'PG_NODE_MALFORMED_DOMAIN_GUARD', '<untagged>', 'flt-block-second']
    example.com -> BLOCK
```

`flt-allow-first` (DIRECT) and `flt-block-second` (BLOCK) both match `example.com`. While both existed the earlier one won. That is first-match-wins, demonstrated rather than assumed.

## 03 — a restart wipes every live rule

`03_probe_state.py` either side of `docker restart node-xray-filtertest`:

```
=== BEFORE restart ===
  tags now      : ['<untagged>', 'PG_NODE_MALFORMED_DOMAIN_GUARD', '<untagged>', 'flt-block-second']
  example.com   : BLOCK

=== restarting the node container ===
  node back up after 2s
  node status: connected after ~2s

=== AFTER restart ===
  tags now      : ['ERR NodeAPIError(code=503, detail=backend not initialized)']
  example.com   : ERR NodeAPIError(code=503, detail=backend not initialized)
```

then, once the backend finished starting:

```
  after ~4s:
    tags now      : ['<untagged>', 'PG_NODE_MALFORMED_DOMAIN_GUARD', '<untagged>']
    example.com   : UNMATCHED
```

Node log for the same window:

```
2026/09/15 00:02:20 IP: 127.0.0.1:61980, Method: Start, Status: Success
2026/09/15 00:02:20 IP: 127.0.0.1:61980, Method: ListBackends, Status: Success
2026/09/15 00:02:22 IP: 127.0.0.1:61980, Method: GetStats, Status: Success
2026/09/15 00:02:25 IP: 127.0.0.1:14744, Method: ListRoutingRules, Status: Success
2026/09/15 00:02:25 IP: 127.0.0.1:14744, Method: TestRoute, Code: Unknown
```

`flt-block-second` is gone and `example.com` is unmatched again. This is the finding that forces the persisted-config path in `../research.md`, consequence 3.

## Honest limits of these runs

- Every routing observation here comes from `TestRoute`, which evaluates the router's decision without opening a connection. None of it proves a real client is blocked, that an unrestricted client's connection survives a live rule change, or how many seconds of unfiltered exposure a restart actually produces. That requires continuous client traffic through the whole cycle and has **not** been run.
- The node reports itself as `connected` to the panel roughly two seconds before its backend can answer routing calls. Any reconciliation that keys off panel status alone will fire into a `503`.
- `list_backends` failed with a bare `ERR backends` in both runs and was not investigated; it is unrelated to the routing question but is not explained.
