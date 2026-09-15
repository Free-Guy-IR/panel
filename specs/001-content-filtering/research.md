# Research: resolving R-1 before planning

**Date**: 2026-09-15
**Question**: can routing rules be changed on a *running* node, so that filtering one endpoint does not require restarting every node attached to the core?
**Answer**: **Yes — after a one-time core change.** Everything below was measured on a disposable Xray node created for this purpose on the test panel, not inferred from source.

## Setup used

| | |
|---|---|
| Panel | test panel, `/root/dev/panel` |
| Node | `filter-test-xray` (panel node id 5), a throwaway container on ports 62950/62951 |
| Core | core 1 "Default Core Config", `type=xray` — one Shadowsocks inbound, outbounds `DIRECT` (freedom) and `BLOCK` (blackhole), one pre-existing rule |
| Xray | 26.3.27 |
| Blast radius | zero — no other node is attached to core 1 |

## The gate nobody had opened

The first attempt failed on every call:

```
NodeAPIError(code=501, detail=unknown service xray.app.router.command.RoutingService)
```

The node exposes `ListRoutingRules`, `AddRoutingRule`, `RemoveRoutingRule` and `TestRoute`, and the Python bridge already wraps all four. None of it works, because the running Xray core does not publish the routing service. The node's config assembly explains why:

```go
var requiredAPIServices = []string{"HandlerService", "LoggerService", "StatsService"}
```

`RoutingService` is recognised (`"routingservice": "RoutingService"` is in the canonical map) but it is **not** on by default. It has to be asked for in the core config:

```json
"api": { "services": ["RoutingService"] }
```

The node merges that with the three required services. Core 1 had no `api` section at all.

**This is the one irreducible restart.** Turning the service on is a core config change, and the panel enforces the blast radius explicitly — `PUT /api/core/{id}` takes `restart_nodes` as a *required* query parameter. So:

- **once per core**: enable `RoutingService`, restart the nodes on it;
- **from then on**: rules are live-editable with no restart at all.

## What was measured, after the gate was open

| Behaviour | Result |
|---|---|
| `ListRoutingRules` | works — returned the 3 live rules |
| `AddRoutingRule` with `should_reset=false` | succeeds on the running core |
| Where the new rule lands | **appended at the end**, index `[3]` |
| Does it take effect immediately | **yes** — `TestRoute` for the target went from unmatched to `BLOCK`, no restart |
| `RemoveRoutingRule` by tag | works — count returns to 3, routing reverts |
| First-match-wins | **confirmed** — see below |
| Duplicate `ruleTag` | **rejected**: `app/router: duplicate ruleTag flt-block-second` |
| Removing a tag that does not exist | **silently accepted**, no error |
| Survival across a node restart | **NO — rules are lost** |
| Calls while the backend is down | `503 backend not initialized` |

### First-match-wins, demonstrated

Two rules were added for the same domain, in order: `flt-allow-first` → `DIRECT`, then `flt-block-second` → `BLOCK`.

```
order: [..., flt-allow-first, flt-block-second]     example.com -> DIRECT
remove flt-allow-first
order: [..., flt-block-second]                      example.com -> BLOCK
```

The earlier rule won while it existed. **An appended rule is evaluated last**, so it only fires for traffic no earlier rule has already claimed.

### Restart wipes live rules

```
before restart   tags: [<untagged>, PG_NODE_MALFORMED_DOMAIN_GUARD, <untagged>, flt-block-second]
                 example.com -> BLOCK
after restart    tags: [<untagged>, PG_NODE_MALFORMED_DOMAIN_GUARD, <untagged>]
                 example.com -> UNMATCHED
```

`AddRoutingRule` mutates the router in memory. It never touches the config the core boots from. Any restart — a crash, an upgrade, an unrelated core edit — silently removes every filter.

## What this means for the design

**R-1 is resolved in favour of live application**, with four consequences that are now requirements on the plan rather than open questions:

1. **Enabling `RoutingService` is a separate, earlier migration.** It restarts nodes, so it is scheduled once per core, deliberately, and is not part of applying a filter. A core without it must be reported as "cannot enforce" rather than silently failing.

2. **Appending is only safe because rules are scoped by `inboundTag`.** A rule that matches only the restricted endpoint cannot be pre-empted by a general rule unless that general rule also matches the same inbound. The plan must check the existing rule list for anything that would match the target inbound *before* the filter, and refuse or warn rather than install a rule that will never fire. Reordering is not available: `AddRoutingRuleRequest` carries exactly one rule, and `should_reset=true` clears **all** rules and balancers, so re-ordering means tearing down and rebuilding the entire rule set on a live node — which is not acceptable.

3. **Reconciliation is mandatory, not a nicety.** Because a restart wipes the rules, FR-016 through FR-020 are load-bearing: the panel must hold the desired state, re-push after any restart, and report a node as *not enforced* until the re-push has been confirmed. A filter that silently disappears is precisely the failure that hurts a parent.

4. **`ruleTag` is the primary key.** Duplicates are rejected, so tags must be deterministic and unique per profile-and-scope. Deletion is idempotent but gives no feedback, so the panel must verify removal by listing rather than by trusting the call.

## Bonus finding

`TestRoute` does exactly what FR-025 asks for: it answers "would this destination be blocked for this endpoint?" without connecting a client. An unmatched destination surfaces as `500 common: not enough information for making a decision` — that is the *no rule matched* signal, not a failure, and the panel must translate it rather than show it as an error.

## Still open

- **R-2** category-data uniformity on remote nodes — unchanged, still unproven off the two accessible servers.
- **R-3 / R-4** the cost of turning on name recovery, and how much ordinary traffic turns out to be unclassifiable. Needs measurement on a real endpoint.
- **R-5** whether a restricted customer's credentials are currently accepted fleet-wide. Not yet examined.
