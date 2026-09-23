# Feature Specification: Close Open Audit Items (panel v5.34.25)

**Feature Branch**: `release/v5.34.25` (spec directory `specs/003-close-open-audit-items`)

**Created**: 2026-09-23

**Status**: Draft (clarifications resolved by the spec author under delegated authority)

**Input**: Close every item left OPEN by the 2026-09-23 production audit of v5.34.24 (commit 72ad0e4a5): A9, A6, A3, A2-r, D5, E1, E3, E4b, E6 residuals (a)(b), G5, G2 (deploy-time host ops), pinning tests for B1 and D4, and the reset_admin_usage lost update. Out of scope: B2, D6, D8, D10, D11, D12, E7, G6, A11, F2c, G1. Patch bump to v5.34.25 only.

## Clarifications

### Session 2026-09-23

The operator delegated every decision to the spec author; each is resolved with the safest option.

- Q: A2-r — make chart and billing atomic, or accept the loss? → A: Atomic. The chart upsert (without fenced users' rows), the billing update and the admin update for one cohort run in ONE retried transaction whose first statement locks the cohort's user rows (users → chart rows, the same order as user deletion, so no new lock-order inversion). A failed cohort is rolled back as a unit and is therefore always safe to retry; it is retained for the existing 10 attempts and only then abandoned at ERROR. Reason: the billing ledger is the money; an accepted trade-off leaves under-billing that is invisible except in logs, while the only cost of atomicity is a longer transaction on at most one cohort (8 nodes).
- Q: A9 — which users are limited in the tick, and what about users with a next plan? → A: Users just billed in the tick whose status is active or on_hold, whose data limit is positive and whose used traffic reached it. They go through the SAME helper the review job uses for limiting, including its next-plan routing (a user with a next plan gets the next plan applied instead of being limited, exactly as the review job would do a few minutes later). Reason: excluding next-plan users would create a second, divergent rule; reusing the helper makes the tick an earlier run of the existing rule, and the review job stays as the backstop.
- Q: A6 — grace value? → A: 30 seconds on the scheduler defaults.
- Q: A3 — what counts as "connection unchanged"? → A: Every field the bridge is built from except name and usage coefficient. If only those two differ, the live bridge is updated in place and no stop/restart reaches the node.
- Q: E1 — what happens to a WireGuard keypair sent by a client for a user without WireGuard access? → A: It is ignored; whatever the panel already stored is left as it is; the panel mints a fresh unique keypair when access is granted (existing behaviour).
- Q: E3 — how is uniqueness made race-free without a users-table migration? → A: A transaction-scoped named lock row in a new, tiny fork table (created by an Alembic migration that does not touch the users table). The lock is taken before any guarded-secret uniqueness check and released by the writer's commit or rollback. Because request sessions on MySQL/MariaDB run REPEATABLE READ and may have opened their snapshot before taking the lock, the holder lookup that follows the lock reads committed data on a fresh READ COMMITTED connection, so a value committed by the previous lock holder is always visible. Works on MySQL, MariaDB, PostgreSQL, TimescaleDB and SQLite (on SQLite the lock write takes the database write lock). Rejected: MySQL GET_LOCK (session-scoped; with pooled async sessions the release can run on a different connection and leak the lock) and a unique index or generated column on users (users-table rebuild; see the MySQL FK-migration OOM incident).
- Q: E4b — what happens when a client supplies an MTProto secret that another user holds? → A: The same rule as the 8 guarded secrets today: the colliding value is replaced by a freshly generated unique secret, a WARNING is logged, and the request succeeds; only when no unique value can be produced after the existing 5 attempts is the request refused with 409. Reason: the reseller bot is known to send template values (the WireGuard template-key case), so refusing would break every bot-created MTProto user, and one consistent rule for all guarded secrets is easier to reason about than two.
- Q: D5 — hide or show deleted nodes' traffic? → A: Show it as a "Deleted nodes" series in bars and pie, so the header total equals the sum of what is drawn.
- Q: G2 — where do file logs go? → A: A path on the host-mounted data directory so they survive recreation, with rotation both for Docker's JSON log (100 MB × 5) and for the file log.
- Q: If granting a newly entitled secret fails while a next plan is applied (for example the uniqueness guard runs out of attempts), does the plan still apply? → A: Yes. The plan is applied, the secret is not written, the failure is logged at ERROR with the user id and field, and the protocol answers 409 until an operator activates it. Reason: aborting would stall the whole review batch for every other user, while a missing secret is already fail-closed by the E6 409 guard.
- Q: Which users count as "MTProto-only" for the 409 refusal? → A: A links request whose rendered output would be empty while at least one MTProto link was skipped because the user has no secret. A user with any other deliverable link keeps receiving those links (the skipped MTProto link is only logged). Reason: refusing a user who has working links would break subscriptions that work today.
- Q: What does a writer get if it cannot take the secret lock in time? → A: The database's own lock-wait timeout applies; the write fails and nothing is stored; user create/modify/bulk-create and bulk-activation endpoints answer HTTP 503 (retryable), other callers keep their existing error mapping. Reason: failing closed is the only choice that can never store a duplicate; the wait is bounded by the database timeout.
- Q: Where does the deleted-nodes series sit and what colour is it? → A: Stacked last (top of each bar, last pie slice) in the theme's muted neutral colour, with its own legend entry; it is omitted when its total is zero. Reason: it must be visible without being mistaken for a live node.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Billing and quota enforcement are exact and prompt (Priority: P1)

A reseller's customer runs past the data limit. The panel limits the customer in the same usage tick that crossed the limit, not a review interval later; no tick is skipped because the scheduler ran late; no billed byte disappears because one write in a cohort failed; and resetting an admin's usage never swallows bytes billed while the reset was in progress.

**Why this priority**: A9 is the only MEDIUM backend item and directly costs the operator money (6-10% of the overshoot); A2-r and the admin reset lose billed bytes silently.

**Independent Test**: With the recorder harness, bill a user across the limit in one tick and assert the user is limited and removed from nodes before the tick returns; make the cohort's write fail once and assert the bytes are billed exactly once on the retry; interleave a usage tick inside an admin reset and assert the tick's bytes survive.

**Acceptance Scenarios**:

1. **Given** an active user with a 1 GB limit at 0.9 GB, **When** a tick bills 0.2 GB, **Then** the user is limited, nodes receive the removal and a status-change notification is emitted, within that tick.
2. **Given** a user with a next plan crossing the limit, **When** the tick bills, **Then** the next plan is applied in that tick exactly as the review job applies it (same reset, same node push, same next-plan notification), and the user is not limited.
3. **Given** a job that starts up to 30 s late, **When** the scheduler fires it, **Then** it runs instead of being skipped.
4. **Given** a cohort whose combined write fails after chart rows were written, **When** the tick ends, **Then** neither chart nor billing holds the bytes and the next tick bills them exactly once.
5. **Given** an admin whose used traffic is being reset, **When** a usage tick adds bytes between the read and the write of the reset, **Then** the admin's counter afterwards equals the tick's bytes and the usage log records the pre-reset value.

---

### User Story 2 - Proxy secrets are unique and match entitlement (Priority: P1)

Every guarded proxy secret is held by at most one user even under concurrent requests; clients cannot plant WireGuard keys on users without WireGuard access; L2TP bulk activation follows the same guarded path as MTProto and OpenVPN; a supplied MTProto secret that collides is replaced by a fresh unique one, as for the other guarded secrets; moving a user to the next plan grants any secret the new groups entitle; and an MTProto-only user without a secret gets an explicit conflict instead of an empty subscription.

**Why this priority**: 3,056 production users share 3 WireGuard keys today (none WireGuard-entitled yet), and the remaining unguarded paths are how duplicates can arise.

**Independent Test**: Run concurrent creates with the same client-chosen guarded value and assert no two users store it; reconcile WireGuard subnets over users sharing a key and assert every WireGuard-bound user ends with a unique key; bulk-activate L2TP and assert unique passwords and no lost concurrent edit.

**Acceptance Scenarios**:

1. **Given** two requests writing the same value to any guarded secret path at the same time, **When** both commit, **Then** the value is stored for at most one user; the other request either receives a regenerated unique value (the existing rule) or is refused, and never stores a duplicate.
2. **Given** WireGuard-bound users that hold a shared key, **When** subnets are reconciled, **Then** each ends with a key no other user holds.
3. **Given** a user without WireGuard access, **When** a client creates or modifies it with a WireGuard keypair, **Then** the keypair is not stored.
4. **Given** L2TP-entitled users without a password, **When** bulk L2TP activation runs, **Then** each receives a unique password through the guarded per-key update, and a concurrent edit to another field of the same user is not lost.
5. **Given** a client supplies an MTProto secret another user holds, **When** the user is created or modified, **Then** the stored secret is a fresh unique value and a WARNING names the collision; 409 is returned only if no unique value can be produced.
6. **Given** a user whose next plan moves it into groups entitling a new secret, **When** the next plan is applied, **Then** the missing secret is granted.
7. **Given** an MTProto-only user with no secret, **When** its links are requested, **Then** the panel answers 409 as it does for L2TP and OpenVPN.

---

### User Story 3 - Renaming a node or changing its coefficient does not disconnect users (Priority: P2)

The operator edits only a node's name or usage coefficient; the node keeps running, users stay connected, and no counters are lost to a core restart.

**Independent Test**: Update a node with only a new name and a new coefficient and assert no stop is sent to the node and the live bridge carries the new values; change the address and assert the old bridge is shut down as before.

**Acceptance Scenarios**:

1. **Given** a connected node, **When** only name and/or usage coefficient change, **Then** no stop/restart is issued and the next tick bills with the new coefficient.
2. **Given** a connected node, **When** any connection field changes, **Then** the node is rebuilt exactly as today.

---

### User Story 4 - The all-nodes chart adds up (Priority: P2)

The operator opens the all-nodes usage chart. Traffic recorded on nodes that were later deleted is shown as a "Deleted nodes" series in the bars and in the pie, so the header total equals what is drawn. Every shipped language has the label.

**Independent Test**: A unit test feeds the chart's data builder a response containing key "0" and asserts bars, pie and total agree; a second test feeds an aggregate-only response and asserts no fake per-node split is produced (D4 pin).

**Acceptance Scenarios**:

1. **Given** usage exists for a deleted node, **When** the chart renders, **Then** a "Deleted nodes" series appears in bars and pie and the header total equals the sum of all drawn series.
2. **Given** no deleted-node usage, **When** the chart renders, **Then** no "Deleted nodes" series appears.

---

### User Story 5 - Production logs survive redeploys and stay bounded (Priority: P3)

After the v5.34.25 deploy, panel logs are written to a file on the host data directory and Docker's own log is rotated, so recreating the container no longer erases history and the disk cannot fill with logs.

**Independent Test**: At deploy time, recreate the panel container and confirm the log file still holds pre-recreate lines, and `docker inspect` shows max-size 100m / max-file 5.

**Acceptance Scenarios**:

1. **Given** the deployed panel, **When** the container is recreated, **Then** earlier log lines remain readable on the host.
2. **Given** heavy logging, **When** limits are reached, **Then** logs rotate and total log storage stays bounded.

---

### User Story 6 - Fixed items stay fixed (Priority: P3)

Regression pins exist for B1 (relative owner-change update), D4 (no fake per-node split), G5 (purge endpoint over HTTP) and every item above; each fails on v5.34.24 behaviour.

**Acceptance Scenarios**:

1. **Given** the B1 test, **When** admin used traffic is incremented between the ownership read and write, **Then** the increment is preserved.
2. **Given** the G5 test, **When** the purge endpoint is called over HTTP by the owner and by a non-owner, **Then** the owner's purge deletes the targeted rows and the non-owner gets 403.

### Edge Cases

- The same-tick limit pushes to nodes only the users whose status or plan changed in that tick; users of admins whose users are already blocked are filtered by the existing node-sync rule, unchanged.
- An A9 limit failure must not undo the billing already committed; it is logged and the review job remains the backstop.
- A cohort that fails 10 times is abandoned with an ERROR naming bytes and nodes (unchanged bound).
- The E3 lock must not be held across a node push or any network call: every guarded write commits before its node push.
- A single-user modify that already holds its user row can deadlock with a bulk activation holding the secret lock; the database aborts one of them, the loser stores nothing and reports an error, and a retry succeeds.
- An ambiguous commit in the usage tick is abandoned with an ERROR, not retried (see SC-002).
- A WireGuard grant and a client keypair in the same request: the grant path decides (shared keys refused, as today).
- D5 when the backend returns only aggregate data: the D4 empty state stays.
- D5 deleted-nodes series with a zero total is not drawn and has no legend entry.
- E6a grant failure during a next plan: plan applied, secret absent, ERROR logged, 409 on that protocol until activation.
- E6b user with MTProto and other links, MTProto secret missing: other links are served; no 409.
- G2 file log path must exist and be writable inside the container before the recreate.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001 (A9)**: The usage tick MUST, before it returns, apply the review job's limit rule (status change, node removal, notification, or next-plan activation) to every user it billed that is active or on_hold, has a positive data limit and has reached it. A failure here MUST NOT undo committed billing and MUST be logged.
- **FR-002 (A6)**: Scheduled jobs MUST tolerate up to 30 s of lateness without being skipped; existing coalesce and single-instance behaviour MUST stay.
- **FR-003 (A2-r)**: For one cohort the chart write (fenced users excluded), the billing and the admin update MUST commit together or not at all, and a failed cohort MUST be retried up to the existing bound.
- **FR-004 (reset_admin_usage)**: Resetting admin usage MUST subtract exactly the logged value relative to the stored counter.
- **FR-005 (A3)**: A node update that changes only name and/or usage coefficient MUST NOT stop, restart or rebuild the node connection.
- **FR-006 (E1)**: WireGuard subnet reconciliation MUST regenerate any shared key for the users it processes; client-supplied WireGuard keypairs for users without WireGuard access MUST NOT be stored.
- **FR-007 (E3)**: Writing any of the 8 guarded secret paths (plus the MTProto secret per FR-009) MUST be serialized so that a check-then-write race cannot store a duplicate, without a users-table schema change. A writer that cannot obtain serialization within the database lock timeout MUST fail without storing anything; user create, modify, bulk create and bulk activation endpoints answer HTTP 503, other paths keep their existing error mapping.
- **FR-008 (E4b)**: Bulk L2TP activation MUST use the guarded entitled-secret path.
- **FR-009 (E4b)**: The MTProto secret MUST join the guarded set: a supplied value held by another user is replaced by a fresh unique value on create and modify (WARNING logged), with 409 only after the existing regeneration attempts are exhausted.
- **FR-010 (E6a)**: Applying a next plan MUST grant every secret the resulting groups entitle; a grant failure MUST NOT block the plan and MUST be logged at ERROR.
- **FR-011 (E6b)**: A links request whose output would be empty because MTProto links were skipped for a missing secret MUST return 409 with the same message family as L2TP/OpenVPN; users with other deliverable links are unaffected.
- **FR-012 (D5)**: The all-nodes chart MUST show deleted-nodes traffic as its own series in bars and pie with a localized label in every shipped locale, and the header total MUST equal the drawn sum.
- **FR-013 (G5, B1, D4)**: Pinning tests MUST exist for the purge endpoint over HTTP, the B1 relative update and the D4 aggregate-only branch.
- **FR-014 (G2)**: The production panel host MUST rotate Docker JSON logs for the panel service (json-file, max-size 100m, max-file 5) and persist panel logs to a rotating file on the host-mounted data directory, applied at the v5.34.25 deploy by the main session. Nothing host-specific is committed.
- **FR-015**: The release MUST be tagged v5.34.25-<slug> (git tag and ghcr image tag), the next patch after v5.34.24-usage-integrity. No in-repo version string changes: pyproject.toml and app/version.py carry upstream's 5.4.1 and stay untouched.
- **FR-016 (ledger)**: OVERRIDES.md MUST list every upstream-tracked file this fork overrides, so the upstream-inventory ledger gate passes. At 72ad0e4a5 the gate already fails on 8 unlisted files: app/app_factory.py, app/db/crud/wireguard.py, app/operation/system.py, app/telegram/utils/texts.py, dashboard/src/components/charts/all-nodes-stacked-bar-chart.tsx, dashboard/src/features/dashboard/components/dashboard-statistics.tsx, dashboard/src/features/users/dialogs/usage-modal.tsx and tests/api/conftest.py. Any upstream file newly overridden by this release is added too.

### Key Entities

- **Usage cohort**: bytes read from up to 8 nodes, billed as one unit.
- **Guarded secret path**: a proxy-settings value that must be unique across users (8 existing paths + MTProto secret).
- **Named lock**: a single row per lock name in a fork table, used only to serialize secret writers.
- **Deleted-nodes series**: usage whose node was deleted (reported under node key 0).

## Success Criteria *(mandatory)*

- **SC-001**: A user crossing the limit is limited within the same tick in 100% of harness runs; the review job is no longer the first limiter.
- **SC-002**: Zero billed bytes lost for a cohort whose write fails fewer than 10 times, except an ambiguous commit (connection lost while the commit was in flight), which is abandoned and logged at ERROR with its byte count because a retry could bill it twice.
- **SC-003**: Zero duplicate values on any guarded secret path after concurrent same-value writers (50 on server databases, 10 on SQLite), on every CI database backend.
- **SC-004**: Renaming a node or changing its coefficient causes zero user disconnections.
- **SC-005**: The chart header total equals the sum of drawn bars and pie slices in 100% of rendered periods.
- **SC-006**: Panel logs from before a container recreate remain readable afterward; total log storage stays under about 1.3 GB (Docker 5 × 100 MB plus the file log at its default 10 MB × 72 rotation).
- **SC-007**: Every new test fails on the unfixed behaviour (v5.34.24 code, or for the B1, D4 and G5 pins a scratch revert of the already-fixed line) and passes on v5.34.25 in TEST_FROM=local and TEST_FROM=github on PostgreSQL; the E3 snapshot case is proven on MySQL, the only backend whose request isolation exposes it.

## Assumptions

- Evidence is the 2026-09-23 audit recorded in the knowledge graph (entities "Bug status on production v5.34.24 - …").
- Existing WireGuard sharers (3,056 users, none entitled) are not rewritten; the v5.34.23 grant path regenerates their key if they ever become entitled.
- The review job stays as the backstop for limiting.
- The per-user usage modal (usage-modal.tsx) drops deleted-node traffic from its total AND its drawing consistently, so it undercounts but never disagrees with itself; it is not part of D5 and is left unchanged.
- Pinning tests for items already fixed at 72ad0e4a5 (B1, D4) cannot fail on the base commit; their "fails before the fix" proof is a run against a scratch, uncommitted revert of the fixed line to its v5.34.22 form.
- Production changes happen only at the v5.34.25 deploy, run by the main session after the test server proof.
