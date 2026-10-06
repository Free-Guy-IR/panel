# Research: Close Open Audit Items (v5.34.25)

All facts were read at 72ad0e4a5 on 2026-09-23 through read-only exploration of the worktree and the audit knowledge-graph entities "Bug status on production v5.34.24 - …". No server was touched.

## R1 A2-r: atomic cohort vs accepted trade-off
- Decision: one retried transaction per cohort, covering the user lock, the fence, the chart upsert, the billing and the admin update.
- Rationale:
  - Chart batches currently commit one by one through `safe_execute`, so a replay would double-count them. That is why v5.34.24 gives up once `writes_started`.
  - An all-or-nothing unit makes every retry safe.
  - Locking users before chart rows matches the user-delete cascade order (users → node_user_usages), so there is no new deadlock cycle.
  - `run_in_retried_transaction` already retries 1213/1205/40P01.
- Alternatives:
  - Accepted trade-off. Rejected: it leaves silent under-billing.
  - Per-batch resume with progress tracking. Rejected: more state; an ambiguous commit per batch is still possible; more code for the same guarantee.
- Unmeasured: the transaction duration on production MySQL. Sized for one cohort of at most 8 nodes, measured on the test server (T013).

## R2 A9: which rule, which helper
- Decision: reuse `apply_status_changes(db, users, UserStatus.limited)` for billed users that are active or on_hold with `User.is_limited`, selected with `_review_user_select_stmt()`.
- Rationale:
  - It is the exact code `limit_users_job` runs, including next-plan routing, node sync and notifications.
  - The same tick simply runs it earlier.
- Alternative: a bespoke UPDATE plus `remove_users`. Rejected: it would skip next plans and notifications.

## R3 A6: misfire grace
- Decision: 30 s in `job_defaults`.
- Rationale:
  - The production log shows "was missed by".
  - The CPU cause that justified load shedding was removed in 1519b6bbb.
  - Coalesce and `max_instances=1` still prevent pile-ups.

## R4 A3: in-place bridge update
- Decision: when `_connection_signature` is unchanged, set `bridge.name` and `bridge.get_extra()["usage_coefficient"]` in place.
- Rationale:
  - `PasarGuardNode.name` is a plain attribute.
  - `get_extra()` returns the live `_extra` dict (PasarGuardNodeBridge/controller.py:282-284).
  - The recorder re-reads the coefficient every cycle (A7).
- Alternative: a remote flush before the restart. Rejected: bigger, and not needed when the connection has not changed.

## R5 E3: serialization without a users-table migration
- Decision: a `named_locks` row, locked by UPDATE as the first guarded-write statement, plus holder lookups on a fresh READ COMMITTED connection.
- Rationale:
  - The lock is transaction-scoped on every backend.
  - A fresh connection defeats the MySQL REPEATABLE READ snapshot, which request sessions may already have opened before taking the lock.
- Alternatives:
  - `GET_LOCK`. Rejected: session-scoped, so the pooled release can leak it.
  - `pg_advisory_xact_lock`. Rejected: PostgreSQL only.
  - A unique index or generated column on JSON paths. Rejected: rebuilds users, the OOM precedent.
  - Serializable isolation. Rejected: global behaviour change.
  - Locking reads. Rejected: they lock every scanned users row.
- Migration: an additive CREATE TABLE plus one INSERT. Rehearsed on a production copy on the test server (T048); users untouched, no ALGORITHM concern.

## R6 E1: client keys for non-entitled users
- Decision: keep the stored keypair (empty on create) and ignore the incoming one, in the fork seam `prepare_fork_proxy_settings`.
- Rationale:
  - It stops new sharers (91 per 24 h) without rewriting the 3,056 existing rows.
  - The v5.34.23 grant path regenerates a shared key when a user becomes WireGuard-bound.
- Revoke is excluded (2026-09-30): a revoke clears the keys of a user without WireGuard access and gives an entitled user a fresh pair, like upstream. See plan D-E1(b).

## R6b E4b: MTProto collision policy
- Decision: regenerate, like the 8 guarded secrets (uniqueness.py:132-148).
- Rationale: the reseller bot sends template values (the WireGuard precedent), so refusing would break sales; one rule for all guarded secrets.
- Alternative: refuse with 409. Rejected for the same reason.

## R7 E6b: when to 409
- Decision: only when the links output would be empty and an MTProto link was skipped for a missing secret.
- Rationale: it matches the L2TP/OpenVPN refusal semantics without breaking mixed users.

## R8 D5/D4 tests
- Decision: extract a pure helper and test it with `bun:test`, the runner the repo already uses in `subscription-settings-schema.test.ts`; add `bun run test` to dashboard-smoke.yml.
- Rationale:
  - There is no vitest in devDependencies.
  - `bun test` needs no new dependency.

## R9 G2
- Decision:
  - Docker json-file with max-size 100m and max-file 5.
  - `SAVE_LOGS_TO_FILE=true` and `LOG_FILE_PATH=/var/lib/pasarguard/logs/panel.log`.
  - Keep the default RotatingFileHandler of 10 MiB × 72.
- Rationale:
  - `/var/lib/pasarguard` is the mounted volume.
  - The handler already rotates, so no host logrotate is needed.

## R10 Ledger gate
- Fact: `scripts/upstream_inventory.py --base v5.4.1 --head HEAD --fail-on-override-outside-ledger OVERRIDES.md` fails at 72ad0e4a5 with 8 unlisted files.
- Decision: P3 adds them plus this release's new overrides (T041), and T042 re-runs the gate.

## R11 Version
- Fact: the fork version lives only in git and image tags. The latest is `v5.34.24-usage-integrity`.
- Decision: tag `v5.34.25-audit-closure` at release, run by the main session. No in-repo version edits.
