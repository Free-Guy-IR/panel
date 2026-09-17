# Research: resolving R-1 before planning

**Date**: 2026-09-15
**Reproduce**: the scripts that produced every measurement below are committed under `specs/001-content-filtering/experiments/`. Raw captured output is in `experiments/results.md`.
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

2. **Scoping by `inboundTag` is not by itself a defence — activation must HARD FAIL.** A rule scoped to the restricted inbound is still evaluated last, so any earlier rule that also matches that traffic wins. An unscoped catch-all — a rule with no `inboundTag` at all — matches *every* inbound, including the restricted one, and would silently defeat the filter. Reordering is unavailable: `AddRoutingRuleRequest` carries exactly one rule, and `should_reset=true` clears **all** rules and balancers, so re-ordering means tearing down and rebuilding the whole rule set on a live node.

   Therefore the rule is **not** "refuse or warn". Before activating a profile the panel MUST walk the live rule list and determine whether any rule ordered before the filter could match traffic on the target inbound — including every rule that carries no `inboundTag`. If any can, **activation fails and the profile is not marked enforced.** A warning is not acceptable: a warning produces a profile the operator believes is protecting a child while it is inert. The only permitted alternatives are to fail, or to establish effective ordering safely (by making the core's boot config carry the filter rule ahead of the conflicting one, which is a core edit, not a live append).

3. **Restart recovery must be fail-CLOSED, and live application alone cannot provide it.** A restart wipes the live rules, so between the moment a node comes back and the moment the panel has re-pushed and *verified*, the restricted endpoint is up and accepting traffic with no filter at all. Reporting "not enforced" describes that window; it does not close it. A child reconnecting during a node upgrade would simply be unfiltered.

   So live application is the mechanism for *changing* a filter without interrupting anyone; it is **not** the mechanism for *holding* one. The filter rules must also live in the core's boot configuration, so the core comes up already enforcing them and there is no unprotected window. That gives two paths that must both be maintained:

   - **persisted**: the rule is written into the core config, which is what the core loads on boot. Core edits accept `restart_nodes=false`, so persisting does not itself restart anything.
   - **live**: the same rule is pushed with `AddRoutingRule` so the change takes effect immediately on the already-running core.

   Where the two disagree, the persisted config is the source of truth and the live router is reconciled to it. If a node cannot be brought into agreement — it is offline, or the push fails — the restricted endpoint must not be left serving unfiltered traffic: the correct fail-closed action is to stop offering that endpoint (disable it, or stop its users being routed to it) until enforcement is confirmed. Reconciliation must then be proven by listing the live rules back and by a `TestRoute` probe, not by a successful call.

4. **`ruleTag` is the primary key.** Duplicates are rejected, so tags must be deterministic and unique per profile-and-scope. Deletion is idempotent but gives no feedback, so the panel must verify removal by listing rather than by trusting the call.

## Bonus finding

`TestRoute` does exactly what FR-025 asks for: it answers "would this destination be blocked for this endpoint?" without connecting a client. An unmatched destination surfaces as `500 common: not enough information for making a decision` — that is the *no rule matched* signal, not a failure, and the panel must translate it rather than show it as an error.

## What the experiment did NOT establish

Stated plainly, because the difference matters:

- `TestRoute` evaluates the router's decision. It does **not** open a connection. Nothing here proves that a real client is actually blocked, that an unrestricted client keeps its connection alive across a live rule change, or that no packets slip through during reconciliation. Those need continuous traffic from a real client through the whole apply / edit / restart / recover cycle, and that test has not been run.
- The restart test restarted the container and then re-queried. No client was connected during it, so the size of the unprotected window was observed only as "the rules are gone", not measured in seconds of exposure.
- Offline reconciliation was observed only as a `503 backend not initialized` error on a call. The behaviour of a profile whose node is offline at activation time, and what happens when it returns, was not exercised.

## Still open

- **R-2** category-data uniformity on remote nodes — unchanged, still unproven off the two accessible servers.
- **R-3 / R-4** the cost of turning on name recovery, and how much ordinary traffic turns out to be unclassifiable. Needs measurement on a real endpoint.
- **R-5** whether a restricted customer's credentials are currently accepted fleet-wide. Not yet examined.
