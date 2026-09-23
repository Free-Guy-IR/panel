# Tasks: Close Open Audit Items (panel v5.34.25)

**Input**: specs/003-close-open-audit-items/ (spec.md, plan.md, research.md, data-model.md, contracts/behaviour.md, quickstart.md)
**Base**: 72ad0e4a5 on branch `release/v5.34.25`

## Rules for every task

- **Owners.** Every task names its owner: P1, P2, P3, P4 or MAIN (the orchestrating session).
  - An owner edits only the files listed for it in the owner map at the end of this file.
  - No two owners ever touch the same file.
- **Test first, and it must fail first.** Each fix task names the test that proves it.
  - That test is written in an earlier task and must FAIL on the unfixed code. Record the failing assertion line in the owner's hand-off before the fix is written.
  - Some items are already fixed at 72ad0e4a5 (B1, D4). Their pinning test is proven by running it against a scratch revert of the fixed line, which is never committed.
- **Test modes.** A task is done only when its tests pass in all three:
  - `TEST_FROM=local`, which is SQLite with `PRAGMA foreign_keys=ON` where the harness sets it.
  - `TEST_FROM=github` with `SQLALCHEMY_DATABASE_URL` set to the owner's PostgreSQL database (`testdb_p1` … `testdb_p4`).
  - P2 also runs `tests/test_proxy_secret_lock.py` against a local MySQL database (`testdb_p2_mysql`), because the snapshot case exists only there.
  - MAIN also runs the merged tree on MySQL and MariaDB 10.6 in T043.
- **Code style.** No explanatory comments or docstrings in any code or test. Ruff check and ruff format must stay clean.
- **Commits.**
  - Author: `git -c user.name=Free-Guy-IR -c user.email=free-guy-ir@users.noreply.github.com commit`.
  - No Co-Authored-By line and no AI name.
  - No server IP or secret in any committed file.
  - No push by the owners.
- **Knowledge graph.** Every owner logs each finished task to the KG entity "v5.34.25 release 2026-09-23" as it lands.
- **Fork seam.** New behaviour goes in `app/fork/*`. An upstream file is touched only where the defect lives inside it, and its line goes to P3 for OVERRIDES.md.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel with the other [P] tasks of the same phase (different files, no dependency).
- **[Story]**: US1–US6 from spec.md.

---

## Phase 1: Setup (MAIN, then owners)

- [ ] T001 MAIN: create one worktree per owner from `release/v5.34.25`, so that parallel test runs never share `./test.db`:
  - `git -C /Users/mohammadahadpor/Projects/pasarguard/panel worktree add /Users/mohammadahadpor/Projects/pasarguard/work/panel-v53425-pN -b release/v5.34.25-pN release/v5.34.25`, for N = 1..4.
  - Start one local PostgreSQL container and create the databases `testdb_p1` … `testdb_p4`.
  - Start one local MySQL container and create `testdb_p2_mysql`.
  - Each owner runs `make run-migration` against its database and records the baseline `TEST_FROM=local uv run pytest tests -q` summary line in its hand-off.
- [ ] T002 [P] P4: in `/Users/mohammadahadpor/Projects/pasarguard/work/panel-v53425-p4/dashboard`, run:
  - `bun install --frozen-lockfile`.
  - `bun node_modules/typescript/bin/tsc --noEmit -p tsconfig.app.json | grep -c "error TS"`, and record the count. The expected baseline is 578.
  - `bun test src/features/subscriptions/components/subscription-settings-schema.test.ts`, which must pass.

## Phase 2: Foundational

No shared prerequisite. The four owners start in parallel as soon as Phase 1 is done. The only cross-owner dependency is the OVERRIDES.md hand-off to P3 for T041.

---

## Phase 3: User Story 1 - Billing and quota enforcement are exact and prompt (Priority: P1) 🎯 MVP

**Goal**:
- A9: limit a user in the tick that crosses the limit.
- A6: no skipped ticks.
- A2-r: no lost cohort.
- No admin-reset lost update.

**Independent test**: T003–T007 pass in all three modes, and each of them failed first.

### Tests for User Story 1 (write first, must fail before the fix)

- [ ] T003 [P] [US1] P1: create `tests/test_scheduler_misfire_grace.py`.
  - Assert `app.scheduler.scheduler._job_defaults["misfire_grace_time"] == 30`, `["max_instances"] == 1` and `["coalesce"] is True`.
  - Behavioural check: build an `AsyncIOScheduler` with `job_defaults` copied from `app.scheduler.scheduler._job_defaults`. Add a `date` job whose run time is 10 s in the past, start the scheduler, and assert that the job ran. Under APScheduler's default grace of 1 s it is skipped with "was missed by".
  - Expected to fail before T008: the defaults assertion fails with a KeyError, and the behavioural job never runs.
- [ ] T004 [P] [US1] P1: create `tests/test_usage_same_tick_limit.py` on the recorder harness (`recorder_db` from `tests/test_recorder_harness.py`, plus `seed_users_and_nodes` and `CountingNode`). Monkeypatch `app.jobs.review_users.sync_users` and the notification entry point so pushes and notifications are recorded. Cases:
  - (a) An active user with `data_limit=1000` and `used_traffic=900` is billed 200 in one `record_user_usages()` call. When the call returns, the status is `limited`, the user was pushed once and one status-change notification was scheduled.
  - (b) An `on_hold` user crossing the limit is limited in the same way.
  - (c) A user with a `next_plan` crossing the limit gets the next plan applied: `used_traffic` is reset, the status is not `limited`, and the next-plan notification was scheduled.
  - (d) A user below the limit and a user with `data_limit` 0 or NULL are untouched.
  - (e) `enforce_user_limits_now` monkeypatched to raise: the billing is still committed, the exception is logged, and the tick returns normally.
  - Cases (a), (b) and (c) are expected to fail before T009, because the status stays active.
- [ ] T005 [P] [US1] P1: create `tests/test_recorder_atomic_cohort.py` on the recorder harness. Cases:
  - (a) Inject an `OperationalError` into the billing `update(User)` step after the chart upsert statements of the same cohort have executed. After the tick, the cohort's `node_user_usages` rows do not exist, `users.used_traffic` is unchanged, and `_retained_cohorts` holds the cohort with `attempts == 1`. After the next tick, the chart and `used_traffic` each hold the bytes exactly once.
  - (b) Force a chart chunk size of 1 and make the second chunk raise. The result is the same as (a).
  - (c) Fenced user (epoch changed between poll and write): no `node_user_usages` row is ever written for that user, and other users in the cohort are billed.
  - (d) Every attempt fails: after `USAGE_RETAINED_MAX_ATTEMPTS` ticks the cohort is abandoned with an ERROR containing "Gave up".
  - (e) A `DBAPIError` with `connection_invalidated=True` raised from the unit is abandoned at once with an ERROR, and is not retained.
  - (a) and (b) are expected to fail before T010: the chart keeps the bytes, and the log shows "a write had already started".
- [ ] T006 [US1] P1: rewrite the two tests in `tests/test_recorder_reset_reads.py` that pin the old give-up.
  - `test_kept_bytes_are_not_retried_once_a_write_started` (:172) becomes `test_kept_bytes_are_retried_after_a_failed_write`.
  - `test_a_fresh_cohort_whose_write_started_is_given_up` (:256) becomes `test_a_fresh_cohort_whose_write_failed_is_retained`.
  - Both assert retention and exactly-once billing on the next tick. Keep `:200`, `:231` and `:274` unchanged.
  - Expected to fail before T010.
- [ ] T007 [P] [US1] P3: create `tests/test_reset_admin_usage_relative.py`.
  - Session A loads an admin with `used_traffic=1000`.
  - Session B runs `update(Admin).values(used_traffic=Admin.used_traffic + 300)` and commits.
  - Session A calls `reset_admin_usage(db, admin)`. Then `admins.used_traffic == 300` and the new `AdminUsageLogs.used_traffic_at_reset == 1000`.
  - Also: a `limited` admin becomes `active` as before, and an admin with `used_traffic == 0` writes no log.
  - Uses the same SQLite / `TEST_FROM` engine pattern as `tests/test_record_usages.py:45-52`.
  - Expected to fail before T011, because the result is 0.

### Implementation for User Story 1

- [ ] T008 [US1] P1: in `app/scheduler.py`, set `job_defaults={"max_instances": 1, "coalesce": True, "misfire_grace_time": 30}`.
  - Extend the existing pins `tests/test_record_usages.py:1307` (`test_scheduler_defaults_serialize_unflagged_jobs`) and `tests/test_record_usages_volatile_window.py:177` to also assert 30.
  - Proof: T003.
- [ ] T009 [US1] P1: create `app/fork/jobs/usage_limits.py` with `async def enforce_user_limits_now(user_ids: Iterable[int]) -> None`. It:
  - opens `GetDB()`;
  - for each chunk of 500 ids, selects `_review_user_select_stmt()` (imported from `app.db.crud.user`, not edited) with `User.id.in_(chunk)`, `User.status.in_([UserStatus.active, UserStatus.on_hold])` and `User.is_limited`;
  - calls `apply_status_changes(db, users, UserStatus.limited)` (imported from `app.jobs.review_users`, not edited).

  Changes in `app/jobs/record_usages.py`:
  - `apply_fenced_user_usage` also returns the ids of the users it billed.
  - `enforce_user_limits_now(billed_ids)` is called right after billing in `_record_user_usages_impl` (next to `enforce_admin_limits_now`, :1386-1390) and in `_flush_retained_cohorts` (:1293-1297).
  - Each call is wrapped in `try/except Exception: logger.exception(...)`.

  Proof: T004.
- [ ] T010 [US1] P1: make the cohort write atomic in `app/jobs/record_usages.py`, per plan D-A2r. Inside the single `run_in_retried_transaction` of `apply_fenced_user_usage`, the order is:
  1. `SELECT … FOR UPDATE` of the users, ordered by id.
  2. The fence decision.
  3. Filter out chart params of fenced users.
  4. Execute the chart upsert statements built by the existing `record_user_stats_batched` statement builder, on the same `conn`, with the same dialect statements and chunk sizes.
  5. Billing update.
  6. Admin update.

  Cleanup and retry behaviour:
  - Delete `_withdraw_fenced_chart_rows` and the separate chart write in `_persist_collected_usage`.
  - Take `JOB_SEM` once around the unit.
  - In `_keep_or_abandon_retained`, retain every failed cohort until `USAGE_RETAINED_MAX_ATTEMPTS`, except a `DBAPIError` with `connection_invalidated=True`, which is abandoned with an ERROR.
  - Drop `PersistProgress.writes_started` if nothing reads it any more. Otherwise keep it only for the cancellation path at :1373-1377, which must retain.
  - Adjust `tests/test_recorder_chart_fence.py` only where it asserts the removed withdrawal internals. Its end-state assertions (the chart never shows fenced bytes) must stay and pass.

  Proof: T005 and T006, plus the whole recorder suite: `tests/test_record_usages*.py`, `tests/test_recorder_*.py` and `tests/test_usage_coeff_freshness.py`.
- [ ] T011 [US1] P3: in `app/db/crud/admin.py` `reset_admin_usage` (:663-693):
  - Keep `logged = db_admin.used_traffic` and the `AdminUsageLogs(used_traffic_at_reset=logged)` insert.
  - Replace `db_admin.used_traffic = 0` with `await db.execute(update(Admin).where(Admin.id == db_admin.id).values(used_traffic=Admin.used_traffic - logged))`.
  - Keep the status change and the refreshes. The existing docstring stays; add no new one.
  - Proof: T007.
- [ ] T012 [US1] P1: run `tests/test_record_usages*.py`, `tests/test_recorder_*.py`, `tests/test_usage_*.py` and `tests/test_scheduler_misfire_grace.py` in `TEST_FROM=local` and `TEST_FROM=github` (on `testdb_p1`).
  - Commit with the fork identity.
  - Hand to P3 one OVERRIDES.md line each for `app/jobs/record_usages.py` and `app/scheduler.py`, if the ledger does not already list them.
- [ ] T013 [US1] MAIN (test server, after T049): watch the usage job for one hour. The G3 duration lines are at INFO. The item passes only if:
  - p95 tick duration is ≤ 1.5 × the v5.34.24 value recorded in T049;
  - there are 0 MySQL 1205/1213 errors from the recorder.
  
  Otherwise stop the release and report.

**Checkpoint**: US1 is independently shippable.

---

## Phase 4: User Story 2 - Proxy secrets are unique and match entitlement (Priority: P1)

**Goal**: E1, E3, E4b and E6(a)(b). Owner: P2 only.

**Independent test**: T014–T021 pass in all three modes, and each failed first.

### Tests for User Story 2 (write first, must fail before the fix)

- [X] T014 [P] [US2] P2: create `tests/test_wireguard_reconcile_shared_keys.py`. Use an in-memory database fixture in the style of `tests/test_entitled_secrets_activation.py`, with FK pragma on, plus a WireGuard core and a subnet pool.
  - Two WireGuard-bound users store the same public key, and a reconcile changes their peer IPs.
  - After `reconcile_wireguard_subnets(db)`, each holds a distinct key and neither holds the old shared key.
  - A third WireGuard-bound user with a unique complete keypair keeps it unchanged.
  - Expected to fail before T024, because the shared key is kept.
- [X] T015 [P] [US2] P2: create `tests/test_wireguard_client_keys_scope.py`. Use `UserOperation` built as in `tests/test_entitled_secrets_group_paths.py`, with `sync_users` monkeypatched.
  - (a) Create a user without WireGuard access with a client keypair. The stored `proxy_settings.wireguard` has no keys.
  - (b) Modify an existing user without WireGuard access, sending a different keypair. The stored keypair is unchanged.
  - (c) A WireGuard-entitled user with a unique keypair is stored as sent.
  - (d) Revoking a user without WireGuard access keeps its stored keys.
  - (a) and (b) are expected to fail before T025.
- [X] T016 [P] [US2] P2: create `tests/test_named_lock_migration.py`.
  - In `TEST_FROM=github`, after `make run-migration`, the table `named_locks` exists with the row `name='proxy_secrets'`.
  - In both modes, `Base.metadata` contains `named_locks` with `name VARCHAR(64) primary key` and `acquired_at` nullable.
  - Load the new migration module by path. Run its `upgrade()` and then `downgrade()` inside `alembic.runtime.migration.MigrationContext.configure(conn)` plus `alembic.operations.Operations`, on a fresh SQLite file. After upgrade, the table exists with the seed row; after downgrade, it is gone. Do not replay the whole chain.
  - Expected to fail before T022.
- [X] T017 [P] [US2] P2: create `tests/test_proxy_secret_lock.py`. Use a file SQLite database in `tmp_path` for local mode, or `SQLALCHEMY_DATABASE_URL` with NullPool in github mode, following `tests/test_entitled_secrets_race.py`. Cases:
  - (a) **Snapshot case.** Session A reads `users`, which opens its snapshot. Session B stores `trojan.password = X` for another user and commits. A then calls `enforce_unique_proxy_secrets` with `X` supplied. X is regenerated in A's `ProxyTable`, so no duplicate is written after A commits.
  - (b) **Interleave case.** A takes the lock and checks X free. B, in a task, blocks on the lock until A commits, then sees X and regenerates.
  - (c) **Concurrency.** N concurrent creates with the same supplied `vless.id` (N=10 on SQLite, 50 on server databases) store it for at most one user: `SELECT COUNT(*) … GROUP BY` shows no value held twice.
  - (d) **Missing row.** A missing `named_locks` row is seeded on first use.
  - (e) **Lock timeout.** With the lock held by an open transaction and a 1 s lock timeout (`innodb_lock_wait_timeout` / `lock_timeout` / SQLite `timeout`), a user create through `UserOperation` answers 503 and stores nothing.
  - Fail-first, before T023:
    - (a) must fail on MySQL (`testdb_p2_mysql`). It is `skipif` on other dialects, with the reason carried in the test id; no comment.
    - (b) and (c) must fail on PostgreSQL and on SQLite.
- [X] T018 [P] [US2] P2: create `tests/test_mtproto_secret_uniqueness.py`.
  - Creating a user with MTProto access and a supplied `mtproto.secret` held by another user stores a different 32-hex secret and logs a WARNING.
  - Modify behaves the same.
  - With the generator monkeypatched to always return the held value, the create answers 409.
  - Expected to fail before T026, because the duplicate is stored.
- [X] T019 [US2] P2: rewrite `tests/test_bulk_l2tp_activation.py` as database-backed tests in the style of `tests/test_entitled_secrets_activation.py`. Cases:
  - `dry_run` returns the count of missing users.
  - Activation grants passwords that are unique and pass `_valid_l2tp_password`.
  - Users without L2TP access are skipped.
  - An existing password is kept.
  - A concurrent edit of another proxy field of the same user, committed between the candidate query and the put, is preserved.
  - The route requires `users:update`.
  - The preserved-edit and uniqueness cases are expected to fail before T027.
- [X] T020 [P] [US2] P2: create `tests/test_next_plan_entitled_grant.py`.
  - A user whose next-plan template groups entitle OpenVPN, L2TP and MTProto gets the missing secrets from `reset_user_by_next(db, user)`.
  - With `issue_unique_secrets` monkeypatched to raise `ProxySecretUniquenessError`, the plan is still applied (groups and limits changed), no secret is written, and an ERROR names the user id and field.
  - Expected to fail before T028.
- [X] T021 [P] [US2] P2: create `tests/test_mtproto_links_409.py`.
  - (a) An MTProto-only user without a secret: the `links` subscription through `SubscriptionOperation` raises `HTTPException` 409 with the MTProto "has not been activated" message.
  - (b) A user with vless and MTProto but no MTProto secret gets 200 with the vless link.
  - (c) A user with a secret gets the MTProto link.
  - (a) is expected to fail before T029, because the response is 200 and empty.

### Implementation for User Story 2

- [X] T022 [US2] P2: create the lock table.
  - Model: `app/fork/models/named_lock.py`, `NamedLock` with `__tablename__ = "named_locks"`, `name: VARCHAR(64) primary key`, `acquired_at: DateTime nullable`. Export it from `app/fork/models/__init__.py`.
  - Migration: `app/db/migrations/versions/<new_rev>_named_locks.py` with `down_revision = "d8b2c4f60a17"`. Upgrade creates the table and inserts `('proxy_secrets', NULL)`; downgrade drops the table.
  - `make check-migrations` must be clean.
  - Proof: T016.
- [X] T023 [US2] P2: create `app/fork/proxy_secrets/locks.py` with `async def acquire_named_lock(db, name)`. It:
  - runs inside `db.no_autoflush`;
  - executes the `UPDATE named_locks SET acquired_at=:now WHERE name=:name` fast path;
  - if `rowcount == 0`, runs the dialect insert-ignore (the pattern at `app/db/crud/wireguard.py:423-440`) and then the UPDATE again;
  - is idempotent per transaction through `db.info`.

  Other changes:
  - Call it first in `enforce_unique_proxy_secrets` (`app/fork/proxy_secrets/uniqueness.py`) and `issue_unique_secrets` (`app/fork/proxy_secrets/entitled.py`).
  - Make `secret_holder_counts` and `secret_values_in_use` read on a fresh connection from `db.get_bind()` with `isolation_level="READ COMMITTED"` on mysql/mariadb only. PostgreSQL and SQLite keep reading on the session.
  - Map lock-wait timeouts to 503:
    - MySQL 1205.
    - PostgreSQL SQLSTATE 55P03, which `lock_timeout` raises as `lock_not_available`.
    - SQLite "database is locked".
    
    Map them in `UserOperation._prepare_user_proxy_settings` (`app/operation/user.py:449-475`, next to the existing 409) and in the bulk activation methods of `app/fork/operation/user_extras.py`.

  Proof: T017, plus the existing `tests/test_proxy_secret_*.py` and `tests/test_entitled_secrets_*.py`.
- [X] T024 [US2] P2: in `app/db/crud/wireguard.py` `reconcile_wireguard_subnets` (:644-650), compute `shared = await shared_wireguard_public_keys(db, users)` for each loaded chunk and call `_ensure_wireguard_keys(user, shared)`. Proof: T014, plus `tests/test_wireguard_shared_key_regeneration.py`.
- [X] T025 [US2] P2: create `app/fork/proxy_secrets/wireguard_scope.py` with `async def drop_unentitled_wireguard_keys(db, proxy_settings, groups, *, exclude_user_id=None) -> ProxyTable`.
  - When `user_has_wireguard_access(db, groups)` is false, it sets `proxy_settings.wireguard` to the stored value of `exclude_user_id`, or to the empty default on create.
  - Call it first in `prepare_fork_proxy_settings` (`app/fork/operation/user_extras.py:35-50`).
  - Proof: T015.
- [X] T026 [US2] P2: add `ProxySecretField.mtproto_secret = "mtproto.secret"` and its `SecretFieldSpec("mtproto", "secret", <token_hex(16) generator>)` in `app/fork/proxy_secrets/fields.py`.
  - Check that `app/fork/proxy_secrets/repair.py` and the `BulkRepairProxySecrets` schema accept the new field.
  - Keep format validation in `app/utils/mtproto.py`.
  - Proof: T018.
- [X] T027 [US2] P2: replace the body of the L2TP bulk activation in `app/fork/operation/user_extras.py:54-97` with `return await self._bulk_activate_entitled_secret(db, bulk_model, EntitledSecretField.l2tp_password)`.
  - Delete `get_users_for_l2tp_activation` from `app/fork/crud/bulk.py` if nothing else references it.
  - Keep the response model unchanged.
  - Proof: T019.
- [X] T028 [US2] P2: add `async def grant_entitled_secrets_after_plan_change(db, db_user)` to `app/fork/proxy_secrets/entitled.py`.
  - It calls `grant_entitled_secrets(db, [db_user])`, catches `ProxySecretUniquenessError` and logs ERROR with the user id and fields.
  - Call it with a local import in `app/db/crud/user.py` `reset_user_by_next`, immediately after `sync_user_allocations` (:1357). That is the only edit to this file.
  - Proof: T020.
- [X] T029 [US2] P2: record per render whether an MTProto link was skipped for a missing secret. Use `_note_unissued_secret` in `app/fork/subscription/helpers.py`.
  - In the fork links path (`app/fork/subscription/links.py`), raise `UnissuedSecretError` for MTProto when the final links output is empty and that flag is set.
  - If `StandardLinks.render` has no seam, add one minimal call in `app/subscription/links.py` and send its OVERRIDES.md line to P3.
  - Proof: T021, plus `tests/test_entitled_secrets_subscription.py` and `tests/test_subscription_*.py`.
- [X] T030 [US2] P2: run all of `tests/` in `TEST_FROM=local` and `TEST_FROM=github` (on `testdb_p2`), and fix any regression in P2 files. Then commit. Hand P3 the OVERRIDES.md lines for every upstream file P2 changed: `app/db/crud/wireguard.py`, `app/db/crud/user.py`, `app/operation/user.py`, and `app/subscription/links.py` if touched.

**Checkpoint**: US2 is independently shippable once the migration is in.

---

## Phase 5: User Story 3 - Renaming a node or changing its coefficient does not disconnect users (Priority: P2)

**Goal**: A3. Owner: P3.

- [ ] T031 [P] [US3] P3: create `tests/test_node_update_in_place.py`.
  - Setup: monkeypatch `app.node.create_node` to return fake bridges that record constructions and `stop()` calls. Each fake has a `name` attribute and an async `get_extra()` that returns its dict.
  - Also monkeypatch `ensure_bridge_memory`/`get_bridge_memory` to no-ops.
  - Call `NodeManager().update_node(node)`, then call it again with only `name` and `usage_coefficient` changed. Expect: one construction, zero `stop()` calls, the same object returned, and that object carrying the new `name` and `get_extra()["usage_coefficient"]`.
  - Changing `address` gives two constructions and one `stop()` on the old bridge.
  - Unchanged fields: the same object, no construction.
  - Expected to fail before T032, because a second construction and a stop happen.
- [ ] T032 [US3] P3: in `app/node/__init__.py` `update_node` (:92-99), when the signature is equal, update `existing.name` and `(await existing.get_extra())["usage_coefficient"]` in place and return `existing`. Proof: T031, plus `tests/api/test_node.py` and `tests/test_node_*.py`.

---

## Phase 6: User Story 4 - The all-nodes chart adds up (Priority: P2)

**Goal**: D5 and the D4 pin. Owner: P4.

- [ ] T033 [P] [US4] P4: create `dashboard/src/fork/charts/all-nodes-series.test.ts` (`bun:test`).
  - **D5**:
    - `statsByNode` holds nodes 1 and 2 plus key `'0'`, 5 GB. The result has series `[node1, node2, __deleted_nodes__]`, where `__deleted_nodes__` is last.
    - Every row carries the `'0'` value.
    - `pieSlices` includes the deleted-nodes slice.
    - `totalBytes` equals the sum of every drawn series and equals the pie total.
    - When `'0'` sums to 0, there is no deleted series.
  - **D4**: when only `'-1'` is present, `rows` is `[]`, `perNodeBreakdownUnavailable` is `true`, `totalBytes` equals the measured sum, and nothing is divided by the node count.
  - **Locales**: load `dashboard/src/fork/locales/{en,fa,ru,zh}.json` and assert that `statistics.deletedNodes` is a non-empty string in each.
  - Fail-first proof:
    - D5 fails against a helper that copies the current inline logic (T034 step 1).
    - The locale case fails before T035.
    - D4 is proven against a scratch revert to the upstream even split, which is not committed.
- [ ] T034 [US4] P4:
  1. Move the current inline memo of `dashboard/src/components/charts/all-nodes-stacked-bar-chart.tsx` (:331-393, and the pie at :414-438) unchanged into `dashboard/src/fork/charts/all-nodes-series.ts` as `buildAllNodesSeries(statsByNode, nodeList, labelFor)`. Confirm the D5 cases of T033 fail.
  2. Implement the deleted-nodes series per plan D-D5: stacked last, colour `hsl(var(--muted-foreground))`, label `t('statistics.deletedNodes')`, and totals taken from drawn series only.
  3. Make the component render the extra series in the area and bar views, the legend, the tooltip and the pie, through the helper's `seriesKeys` and `pieSlices`.
  
  Proof: T033.
- [ ] T035 [P] [US4] P4: add `"statistics.deletedNodes"` to every shipped locale overlay:
  - en "Deleted nodes" in `dashboard/src/fork/locales/en.json`
  - fa "نودهای حذف‌شده" in `dashboard/src/fork/locales/fa.json`
  - ru "Удалённые узлы" in `dashboard/src/fork/locales/ru.json`
  - zh "已删除的节点" in `dashboard/src/fork/locales/zh.json`
  
  `dashboard/public/statics/locales/*.json` stay byte-identical to upstream. Proof: a `bun:test` case in T033 loads all four overlays and asserts the key is non-empty in each.
- [ ] T036 [US4] P4: finish the dashboard wiring.
  - Add `"test": "bun test src"` to `dashboard/package.json` scripts.
  - Add a step `bun run test` after `bun install --frozen-lockfile` in `.github/workflows/dashboard-smoke.yml`.
  - Checks:
    - `bun run test` passes.
    - `bun run build` passes.
    - The tsc error count is ≤ the T002 baseline.
  - Commit, and send P3 the OVERRIDES.md line for the chart file.

---

## Phase 7: User Story 5 - Production logs survive redeploys and stay bounded (Priority: P3)

**Goal**: G2. Owner: MAIN, at the v5.34.25 production deploy only. Nothing from this phase is committed.

- [ ] T037 [US5] MAIN (production deploy window, after T052): on the production panel host, run the D-G2 steps of plan.md in order:
  1. Back up `/opt/pasarguard/docker-compose.override.yml` and `/opt/pasarguard/.env` as `*.bak-v53425`.
  2. `mkdir -p /var/lib/pasarguard/logs`.
  3. Add the panel-service `logging` block: json-file, `max-size: "100m"`, `max-file: "5"`.
  4. Set `SAVE_LOGS_TO_FILE=true` and `LOG_FILE_PATH=/var/lib/pasarguard/logs/panel.log`.
  5. Check `docker compose config`.
  6. Recreate together with the image switch.
- [ ] T038 [US5] MAIN: verify the log change. The item passes only if:
  - `docker inspect` of the panel container shows `LogConfig.Config` with `max-size=100m` and `max-file=5`;
  - `panel.log` grows;
  - after a second `docker compose up -d --force-recreate` of the panel service, lines written before it are still in `panel.log`.
  
  Rollback is to restore both backups and recreate. Record it in the KG.

---

## Phase 8: User Story 6 - Fixed items stay fixed (Priority: P3)

**Goal**: pinning tests for G5 and B1 (P3). The D4 pin is in T033.

- [ ] T039 [P] [US6] P3: create `tests/api/test_traffic_log_purge_http.py` with the `client` from `tests/api` and the existing auth helpers in `tests/api/helpers.py`.
  - Seed `TrafficLogRecord` rows, both old and recent.
  - The owner calls `POST /api/traffic-log/purge` with `{"older_than_hours": 1}`: the response is 200, `removed` equals the number of old rows, and the recent rows remain.
  - A body-less call removes every row.
  - A non-owner admin with `nodes` permission gets 403, and no row is removed.
  - Fail-first: run it against a scratch copy of `app/fork/routers/traffic_log.py` where `purge_before` is replaced by a no-op, which is never committed. It must fail on `removed`.
- [ ] T040 [P] [US6] P3: create `tests/test_set_owner_relative_update.py`.
  - Session A loads user U (`used_traffic=500`) owned by admin X (`used_traffic=2000`), plus admin Y.
  - Session B adds 300 to X through a relative update and commits.
  - A runs the `set_owner` operation that moves U to Y and commits. Then X is 1800 and Y is its old value + 500.
  - Fail-first: run it against a scratch revert of `app/db/crud/user.py:1877-1885` to the v5.34.22 ORM-literal form (`old_admin.used_traffic -= db_user.used_traffic`), never committed. It must fail with X equal to 1500.

---

## Phase 9: Polish, ledger, merge, review, deploy

- [ ] T041 P3: update `OVERRIDES.md`.
  - Add one backticked-path entry with a one-line justification for each of the 8 files that already fail the ledger gate at 72ad0e4a5:
    - `app/app_factory.py`
    - `app/db/crud/wireguard.py`
    - `app/operation/system.py`
    - `app/telegram/utils/texts.py`
    - `dashboard/src/components/charts/all-nodes-stacked-bar-chart.tsx`
    - `dashboard/src/features/dashboard/components/dashboard-statistics.tsx`
    - `dashboard/src/features/users/dialogs/usage-modal.tsx`
    - `tests/api/conftest.py`
  - Also add every new upstream override handed over in T012, T030 and T036.
  - Add `app/node/__init__.py` and `app/db/crud/admin.py` if they are not listed.
- [ ] T042 P3: in a throwaway detached worktree, merge the P1–P4 branches and run `uv run python scripts/upstream_inventory.py --base v5.4.1 --head HEAD --fail-on-override-outside-ledger OVERRIDES.md`.
  - It must exit 0. Remove that worktree afterwards.
  - Commit only P3's own files (T007, T011, T031, T032, T039–T041) on `release/v5.34.25-p3`.
  - MAIN re-runs the gate on the real merge in T044.
- [ ] T043 MAIN: merge `release/v5.34.25-p1..p4` into `release/v5.34.25`. There must be no conflicts, since the files are disjoint. Then run the full suite:
  - `TEST_FROM=local uv run pytest tests -q`;
  - `TEST_FROM=github` on PostgreSQL;
  - `TEST_FROM=github` on MySQL latest and MariaDB 10.6 in local containers, each after `make run-migration` and `make check-migrations`.
- [ ] T044 MAIN: on the merged tree, run:
  - `uv run ruff check .` and `uv run ruff format --check .`;
  - the upstream-inventory ledger gate command from T042, which must exit 0;
  - the dashboard `bun run test`, `bun run build` and the tsc count (≤ baseline).
- [ ] T045 MAIN: check for comments. `git diff 72ad0e4a5..release/v5.34.25 -- '*.py' '*.ts' '*.tsx' '*.yml'` must add no explanatory comment or docstring line, allowing only shebangs, pragmas and region markers. Fix any that appear by sending them back to the owning owner.
- [ ] T046 MAIN: grep the diff for secrets and IPs, sensitive filenames and `Co-Authored-By`. All must be empty.
- [ ] T047 MAIN: run the external review gate on each owner's diff. Three independent reviewers must approve unanimously; report partial approval as partial.
  - Resolve real findings through the owning owner.
  - Then run the whole-release review.
  - The release is not done without every approval.
- [ ] T048 MAIN: rehearse the `named_locks` migration on a copy of production data on the test server. Record the duration and confirm that `users` is untouched: its row count and checksum are equal before and after.
- [ ] T049 MAIN: before deploying, record the test server's p95 usage-tick duration on v5.34.24 from its log. This is the T013 baseline.
  - Build and deploy the v5.34.25 image to the test server.
  - Apply the D-G2 logging block and log-file settings to the test server's compose override and `.env`, and verify them with the T038 checks. G2 is proven there before production.
- [ ] T050 MAIN: prove each item on the test server with real traffic:
  - A9: a test user crosses the limit and is limited within one tick.
  - A3: rename a node and change its coefficient; the node log shows no Stop and connections stay up.
  - D5: the deleted-nodes series appears after deleting a throwaway node.
  - E6b: an MTProto-only user without a secret gets 409.
  - E3/E4b: concurrent same-value creates store no duplicate.
  - E4b: L2TP bulk activation grants unique passwords.
  
  Also complete T013.
- [ ] T051 MAIN: push `release/v5.34.25`, open the PR, and verify CI for the exact head SHA. All five database jobs, the upstream-inventory ledger gate, dashboard-smoke and ruff must be green.
- [ ] T052 MAIN: tag `v5.34.25-audit-closure`, the next patch after `v5.34.24-usage-integrity`. Build and push the image, then deploy to production. Run T037 and T038 in the same window.
- [ ] T053 MAIN: after the production deploy, run the count-only checks from the audit: no duplicate groups on the 9 guarded fields, entitled-without-secret counts, and the sharer count not growing. Record everything in the KG entity "v5.34.25 release 2026-09-23".

---

## Dependencies & Execution Order

- Phase 1 (T001, T002) must finish before anything else.
- P1, P2, P3 and P4 then run fully in parallel.
- Within each owner, the tests come first and must fail. The fix follows, then the owner's full-suite run.
- P3's T041/T042 need the OVERRIDES.md hand-offs from T012, T030 and T036.
- The MAIN sequence is T043 → T044 → T045 → T046 → T047 → T048 → T049 → T050 (with T013) → T051 → T052 (with T037, T038) → T053.

### User Story Dependencies

- US1, US2, US3, US4 and US6 are independent of each other.
- US5 depends only on the production deploy (T052).

### Parallel Example

```text
P1: T003, T004, T005 in parallel → T006 → T008 → T009 → T010 → T012
P2: T014..T018, T020, T021 in parallel → T019 → T022 → T023 → T024 → T025 → T026 → T027 → T028 → T029 → T030
P3: T007, T031, T039, T040 in parallel → T011 → T032 → T041 → T042
P4: T002 → T033 → T034 → T035 → T036
```

## Owner → tasks → files map

| Owner | Tasks | Files (exclusive) |
|---|---|---|
| P1 recorder/scheduler | T003, T004, T005, T006, T008, T009, T010, T012 | `app/jobs/record_usages.py`, `app/scheduler.py`, `app/fork/jobs/usage_limits.py` (new), `tests/test_scheduler_misfire_grace.py` (new), `tests/test_usage_same_tick_limit.py` (new), `tests/test_recorder_atomic_cohort.py` (new), `tests/test_recorder_reset_reads.py`, `tests/test_recorder_chart_fence.py`, `tests/test_record_usages.py`, `tests/test_record_usages_volatile_window.py`, `tests/test_recorder_harness.py` |
| P2 secrets/WireGuard | T014–T030 | `app/db/crud/wireguard.py`, `app/db/crud/user.py` (one call in `reset_user_by_next`), `app/operation/user.py` (503 mapping), `app/fork/proxy_secrets/{uniqueness,entitled,fields,repair,schemas}.py`, `app/fork/proxy_secrets/locks.py` (new), `app/fork/proxy_secrets/wireguard_scope.py` (new), `app/fork/models/named_lock.py` (new), `app/fork/models/__init__.py`, `app/db/migrations/versions/<rev>_named_locks.py` (new), `app/fork/operation/user_extras.py`, `app/fork/operation/entitled_secrets.py`, `app/fork/crud/bulk.py`, `app/utils/mtproto.py`, `app/fork/subscription/helpers.py`, `app/fork/subscription/links.py`, `app/subscription/links.py` (only if no seam), `tests/test_wireguard_reconcile_shared_keys.py`, `tests/test_wireguard_client_keys_scope.py`, `tests/test_named_lock_migration.py`, `tests/test_proxy_secret_lock.py`, `tests/test_mtproto_secret_uniqueness.py`, `tests/test_bulk_l2tp_activation.py`, `tests/test_next_plan_entitled_grant.py`, `tests/test_mtproto_links_409.py` (all new except the L2TP file), plus any existing `tests/test_entitled_secrets_*.py` or `tests/test_proxy_secret_*.py` that needs the new behaviour |
| P3 node/admin/tests/ledger | T007, T011, T031, T032, T039, T040, T041, T042 | `app/node/__init__.py`, `app/db/crud/admin.py`, `OVERRIDES.md`, `tests/test_reset_admin_usage_relative.py` (new), `tests/test_node_update_in_place.py` (new), `tests/api/test_traffic_log_purge_http.py` (new), `tests/test_set_owner_relative_update.py` (new) |
| P4 dashboard | T002, T033, T034, T035, T036 | `dashboard/src/components/charts/all-nodes-stacked-bar-chart.tsx`, `dashboard/src/fork/charts/all-nodes-series.ts` (new), `dashboard/src/fork/charts/all-nodes-series.test.ts` (new), `dashboard/src/fork/locales/{en,fa,ru,zh}.json`, `dashboard/package.json` (scripts only), `.github/workflows/dashboard-smoke.yml` |
| MAIN | T001, T013, T037, T038, T043–T053 | No repo source file. Merges, host config on the production panel host (never committed), tags |

The shared-file rule holds: every path above appears under exactly one owner.
- P1 imports `_review_user_select_stmt` and `apply_status_changes` without editing their files.
- P3's T040 reads `app/db/crud/user.py` but does not edit it.
- A scratch revert for a fail-first proof is never committed.

## Notes

- Upstream files are edited only where the defect lives inside them. Each such file ends up in OVERRIDES.md (T041).
- Out of scope, do not touch: B2 (the owner declined a floor), D6, D8, D10, D11, D12, E7, G6, A11, F2c and G1.
- If a subagent hits a session or usage limit, stop spawning, commit what is green on the owner branch, and report.
