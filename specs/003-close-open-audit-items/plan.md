# Implementation Plan: Close Open Audit Items (panel v5.34.25)

**Branch**: `release/v5.34.25` | **Date**: 2026-09-23 | **Spec**: [spec.md](spec.md)
**Base**: 72ad0e4a5 (tag v5.34.24-usage-integrity, the production commit)
**Input**: specs/003-close-open-audit-items/spec.md

## Summary

v5.34.25 closes every item the 2026-09-23 audit left OPEN on production v5.34.24:

- Backend fixes:
  - A9: same-tick user limiting.
  - A6: misfire grace.
  - A2-r: atomic cohort write.
  - admin-reset lost update.
  - A3: in-place node update.
  - E1: WireGuard shared keys and client keys.
  - E3: serialized secret writes.
  - E4b: L2TP bulk on the guarded path, and MTProto uniqueness.
  - E6: the next-plan grant and the MTProto 409.
- One dashboard fix: D5, the deleted-nodes series.
- Pinning tests: G5, B1 and D4.
- An OVERRIDES.md ledger repair.
- One deploy-time host change: G2, log persistence and rotation. It is specified here and executed by the main session.

Schema changes:
- One additive migration creates a one-row lock table.
- No users-table change.

The work is split over four owners, P1 to P4, with disjoint files.

## Technical Context

**Language/Version**:
- Backend: Python ≥ 3.14 (runtime image 3.14).
- Dashboard: TypeScript/React with Vite, run under Bun.

**Primary Dependencies**:
- FastAPI and SQLAlchemy 2 async.
- APScheduler `AsyncIOScheduler`.
- PasarGuardNodeBridge: `PasarGuardNode.name` is a public attribute, and `get_extra()` returns the live `_extra` dict.
- Dashboard: Recharts, i18next, and `bun:test` for unit tests.

**Storage**: MySQL 9 on production. CI runs five backends: SQLite, PostgreSQL, TimescaleDB (pg17), MySQL latest and MariaDB 10.6 (.github/workflows/test-database-migrations.yml). Each runs `make run-migration`, `make check-migrations` and `make test`.

**Testing**:
- Framework: pytest.
- `TEST_FROM=local` uses SQLite: the recorder harness uses an in-memory StaticPool, and tests/api uses `./test.db`. `TEST_FROM=github` uses `SQLALCHEMY_DATABASE_URL`.
- FK enforcement is on in the recorder harness.
- Dashboard: `bun test`. There is no test script and no CI step today.

**Target Platform**: A Linux Docker container; the panel runs on a single host.

**Project Type**: A web service plus an SPA dashboard.

**Performance Goals**:
- A usage tick must still finish inside its interval.
- The A2-r combined transaction is bounded to one cohort: at most 8 nodes, with chart chunks of 1000 rows on MySQL and 400 on SQLite.

**Constraints**:
- No explanatory comments or docstrings anywhere, tests included.
- Prefer fork seams.
- Tests pass in both TEST_FROM modes and on PostgreSQL.
- Patch bump only.
- No server IPs in committed files.

**Scale/Scope**: 5,380 users, about 3,500 of them holding a WireGuard key; 3,056 sharers on 3 keys; 252 users entitled to WireGuard.

**Facts measured at 72ad0e4a5** (file:line are the owners' starting points):

- **Recorder writes**:
  - Writes go through `run_in_retried_transaction` (app/jobs/record_usages.py:362). It runs `engine.begin()`, uses READ COMMITTED on MySQL, and retries deadlocks and lock waits: 1213, 1205, 40P01 and "locked".
  - Chart batches are written by `record_user_stats_batched` (:469-523). Each `safe_execute` (:436) is its own transaction.
  - Billing is `apply_fenced_user_usage` (:961-1024), one FOR UPDATE transaction. It includes `_withdraw_fenced_chart_rows` (:916-958).
  - The give-up happens in `_keep_or_abandon_retained` (:1241-1259). `USAGE_RETAINED_MAX_ATTEMPTS` is 10.
  - `enforce_admin_limits_now` runs after billing at :1386-1390 and :1293-1297.
- **User limiting**:
  - The helper is `apply_status_changes(db, users, status)` (app/jobs/review_users.py:46). Plain users get a bulk status update, `sync_users` and a notification. Users with a next plan get `reset_user_by_next`, `sync_users` and the next-plan notification.
  - Selection uses `_review_user_select_stmt()` (app/db/crud/user.py:73) plus `User.is_limited` (app/db/models.py:356-361).
- **Scheduler**: app/scheduler.py is one line, `AsyncIOScheduler(job_defaults={"max_instances": 1, "coalesce": True}, timezone="UTC")`. The defaults are pinned by tests/test_record_usages.py:1307 and tests/test_record_usages_volatile_window.py:177.
- **Node update**:
  - `NodeManager.update_node` (app/node/__init__.py:78-111) already computes `_connection_signature`.
  - When the signature is equal but the name or the coefficient differs, it still builds a new bridge and calls `_shutdown_node(old)`, which sends `stop()`.
- **Admin reset**: `reset_admin_usage` (app/db/crud/admin.py:663-693) logs `db_admin.used_traffic` and then assigns the literal `0`.
- **WireGuard**:
  - `reconcile_wireguard_subnets` (app/db/crud/wireguard.py:569-666) calls `_ensure_wireguard_keys(user)` at :650 with no shared set.
  - `shared_wireguard_public_keys(db, users)` is at :249-275 and is used by `sync_users_allocations` at :482/:515.
  - `prepare_wireguard_keys` (app/utils/wireguard.py:44-56) returns client keys unchanged when the user has no access.
  - The fork seam `prepare_fork_proxy_settings` (app/fork/operation/user_extras.py:35-50) runs right after it, with `groups` and `exclude_user_id`.
- **Secret uniqueness**:
  - `enforce_unique_proxy_secrets` and `secret_holder_counts` are in app/fork/proxy_secrets/uniqueness.py:55-153. The latter is a plain SELECT inside the request session, which on MySQL is REPEATABLE READ.
  - The entitled path is app/fork/proxy_secrets/entitled.py: `issue_unique_secrets` (:231-260), `secret_values_in_use` (:220), the conditional put (:190-204) and FOR UPDATE on reload (:307).
  - There are no advisory locks anywhere.
- **L2TP bulk**:
  - app/fork/operation/user_extras.py:54-97 is the old whole-document replace.
  - `_bulk_activate_entitled_secret` is at :153-178. `EntitledSecretField.l2tp_password` exists (entitled.py:34-37).
- **MTProto**: `prepare_mtproto_secret` (app/utils/mtproto.py:53-71) only fills an empty secret. `mtproto.secret` is not in the 8 guarded fields (app/fork/proxy_secrets/fields.py:11-19).
- **Next plan**:
  - `reset_user_by_next` (app/db/crud/user.py:1334-1392) swaps groups at :1356 and calls `sync_user_allocations` at :1357, with no grant.
  - The grant function is `grant_entitled_secrets(db, users, *, fields=None, reserved=None)` (entitled.py:348). It is async, flushes and does not commit.
- **MTProto links**:
  - `_build_mtproto_components` (app/fork/subscription/helpers.py:163-218) notes the unissued secret and returns None.
  - `_build_mtproto` (:220-224) returns `""`.
  - `UnissuedSecretError` is the 409 (:34-41).
  - Links are registered through `register_fork_link_handlers` (app/fork/subscription/links.py), which is called from `StandardLinks.__init__`.
- **Traffic-log purge**: `POST /api/traffic-log/purge` (app/fork/routers/traffic_log.py:197-248) is `OWNER_ONLY`. It is only covered by a route-registration pin (tests/test_fork_boundary.py:86).
- **D5**:
  - dashboard/src/components/charts/all-nodes-stacked-bar-chart.tsx: the bars (:362-376) and the pie (:414-438) use `nodeList` only, while the header total (:381-386) sums every key, including `'0'`.
  - Key `'0'` is NULL `node_id` (SET NULL on node delete) passed through coalesce.
  - Locale overlays live in dashboard/src/fork/locales/{en,fa,ru,zh}.json. The dashboard/public/statics/locales files stay byte-identical to upstream.
- **Ledger gate**: `scripts/upstream_inventory.py --base v5.4.1 --head HEAD --fail-on-override-outside-ledger OVERRIDES.md` fails at 72ad0e4a5, with 8 files unlisted. The check was run locally on 2026-09-23.
- **Version**: The fork version exists only as the git tag and the image tag (latest `v5.34.24-usage-integrity`). pyproject.toml and app/version.py carry upstream's `5.4.1`.
- **File logging**: `LoggingSettings` (config.py:165-174) defaults to a RotatingFileHandler of 10 MiB × 72 when `SAVE_LOGS_TO_FILE=true` (app/utils/logger.py:76-94). docker-compose mounts `/var/lib/pasarguard`.

## Constitution Check

| Principle | Status | How |
|---|---|---|
| I Upstream boundary | PASS with tracked deviations | New behaviour goes in fork modules:<br>• app/fork/jobs/usage_limits.py<br>• app/fork/proxy_secrets/locks.py<br>• app/fork/proxy_secrets/wireguard_scope.py<br>• app/fork/models/named_lock.py<br>• dashboard/src/fork/charts/all-nodes-series.ts<br>Upstream files get minimal call-site edits only where the defect lives inside an upstream function; see Complexity Tracking. Every one of them is added to OVERRIDES.md (T041). A missing lock table fails loudly: the guarded write errors and nothing is stored. |
| II Verified ground | PASS | Every fact above was read at 72ad0e4a5. One thing is not measured: how long the A2-r combined transaction takes on production MySQL. The design is sized for one cohort; T013 measures it on the test server, and deploy is blocked unless p95 tick duration is ≤ 1.5× the v5.34.24 value and there are 0 recorder lock-wait/deadlock errors. |
| III Test server first | PASS | T049 and T050 deploy and prove the fixes on the test server before production, and that includes the G2 logging change applied to the test server. Production gets the same G2 change only in the release deploy (T052, T037, T038). |
| IV Fleet safety | PASS | A3 removes a needless remote Stop. A9 pushes only the users it changes. E3 holds no lock across network calls: the node push runs after commit. |
| V Clean code | PASS | No comments or docstrings (checked by T045). Ruff stays clean. The `tsc` error count must not rise above the 578 baseline (T002, T036, T044). The E3 migration is reversible and is rehearsed on a production copy (T048). |
| VI Security | PASS | No IPs or secrets in the repo. G2 values live only on the host. E3 fails closed. E1 stops clients planting keys. |
| VII Review/record | PASS | The external review gate runs on each owner's diff: three independent reviewers must approve unanimously, and partial approval is reported as partial. A whole-release review follows (T047). The KG is updated per step. Commits are made as Free-Guy-IR. CI is verified after the push by the main session. |

Post-design re-check: still PASS. The E3 table is additive and does not rebuild users.

## Design Decisions

### D-A9 Same-tick user limiting (P1)
- **Change**:
  - `apply_fenced_user_usage` also returns the ids of the users it billed.
  - After `_persist_collected_usage`, and after `_flush_retained_cohorts`, the tick calls the new `enforce_user_limits_now(user_ids)` (app/fork/jobs/usage_limits.py), next to `enforce_admin_limits_now`.
- **What it does**:
  - Opens its own `GetDB()` session.
  - Selects `_review_user_select_stmt()` where `User.id IN chunk` (chunks of 500), `User.status IN (active, on_hold)` and `User.is_limited`.
  - Calls `apply_status_changes(db, users, UserStatus.limited)`. That keeps the review job's next-plan routing and notifications identical.
- **Failure handling**: It is wrapped in `try/except Exception: logger.exception`. Billing is already committed at that point, and the review job remains the backstop.
- **Proof**: tests/test_usage_same_tick_limit.py.

### D-A6 Misfire grace (P1)
`job_defaults={"max_instances": 1, "coalesce": True, "misfire_grace_time": 30}`. No `add_job` call overrides it.

### D-A2r Atomic cohort write (P1)
**Chosen:** one `run_in_retried_transaction` per cohort. The steps inside `_apply(conn)`, in order:
1. `SELECT … FOR UPDATE` of the cohort's users, ordered by id. This already exists.
2. The fence decision (epoch mismatch).
3. Rows of fenced users are filtered out of the chart params, which replaces the withdrawal after the fact.
4. Chart upsert batches run on the same `conn`, using the existing dialect statements and chunk sizes.
5. Billing update.
6. Admin update.

Consequences:
- The lock order is users → node_user_usages, the same order as the user-delete cascade, so there is no new inversion.
- Because the unit commits or rolls back as a whole, `PersistProgress.writes_started` no longer causes a give-up. Every failed cohort is retained, up to 10 attempts.
- `_withdraw_fenced_chart_rows` is deleted once nothing references it.
- An ambiguous COMMIT is abandoned with an ERROR log giving the byte count, and is not retried, because it may have committed. It is detected as `sqlalchemy.exc.DBAPIError` with `connection_invalidated=True`. Every other error happens before the commit and rolls back fully, so it is retried.

**Rejected: documenting it as an accepted trade-off.** The billing ledger is the money. The loss is invisible except in the logs. Separate commits cannot be retried safely, because a replay double-counts chart batches that already committed.

**Tests:**
- test_kept_bytes_are_not_retried_once_a_write_started and test_a_fresh_cohort_whose_write_started_is_given_up pin the old behaviour, so they are rewritten to assert retry-and-bill-once.
- New file tests/test_recorder_atomic_cohort.py.

### D-reset Admin reset (P3)
`reset_admin_usage` keeps writing `AdminUsageLogs(used_traffic_at_reset=logged)`, where `logged = db_admin.used_traffic` as read. It replaces `db_admin.used_traffic = 0` with `update(Admin).where(Admin.id == db_admin.id).values(used_traffic=Admin.used_traffic - logged)`, the same pattern as `set_owner` (crud/user.py:1877-1885). The status un-limit keeps the same condition. The existing refresh then picks up the SQL value.

### D-A3 In-place node update (P3)
In `update_node`, when `existing is not None` and the stored signature equals the new one:
- If name and coefficient are unchanged, return `existing`. This is unchanged.
- Otherwise:
  - set `existing.name = node.name`;
  - set `(await existing.get_extra())["usage_coefficient"] = node.usage_coefficient`;
  - return `existing`.

No `create_node` and no `_shutdown_node`. `_node_usage_coefficient` (record_usages.py:675-724) reads `get_extra()` every cycle, so the next tick bills with the new value. Any signature change keeps today's rebuild path.

### D-E1 WireGuard (P2)
- **(a) Reconcile.** In `reconcile_wireguard_subnets`, compute `shared = await shared_wireguard_public_keys(db, users)` for each loaded chunk and call `_ensure_wireguard_keys(user, shared)`.
- **(b) Client keys.** Add the new `app/fork/proxy_secrets/wireguard_scope.py::drop_unentitled_wireguard_keys(db, proxy_settings, groups, *, exclude_user_id)` and call it first in `prepare_fork_proxy_settings`.
  - It runs only when `user_has_wireguard_access(db, groups)` is false.
  - When it runs, it replaces `proxy_settings.wireguard` with the stored value: `exclude_user_id`'s row, or empty on create.
  - Revoke (`_prepare_revoked_proxy_settings`) calls the same seam, so a non-entitled user keeps its stored keys there too.
  - The existing sharers are not rewritten. They are regenerated on grant by the v5.34.23 path.

### D-E3 Named lock row + fresh read (P2)
- **Table and startup**:
  - The new fork table is `named_locks(name VARCHAR(64) PRIMARY KEY, acquired_at DATETIME NULL)`.
  - The Alembic migration has `down_revision = d8b2c4f60a17`. It creates the table and inserts `('proxy_secrets', NULL)`. Its downgrade drops the table.
  - The model is app/fork/models/named_lock.py, registered in app/fork/models/__init__.py.
  - A missing row is seeded on first use (see Missing row below). A missing table fails loudly as a database error, and nothing is stored.
- **Taking the lock** (app/fork/proxy_secrets/locks.py): `acquire_named_lock(db, "proxy_secrets")` runs under `db.no_autoflush`. It executes `UPDATE named_locks SET acquired_at = :now WHERE name = :name` and asserts `rowcount == 1`.
  - MySQL, MariaDB, PostgreSQL and TimescaleDB: this takes a row X-lock until commit or rollback.
  - SQLite: it takes the RESERVED write lock.
- **Holder lookup on a fresh read**:
  - On mysql/mariadb only, `secret_holder_counts` and `secret_values_in_use` run on a separate `engine.connect()` with `isolation_level="READ COMMITTED"`.
  - On PostgreSQL and TimescaleDB, request sessions are already READ COMMITTED, so the in-session read after the lock sees every committed value.
  - On SQLite, the read stays on the session's own connection. SQLite cannot silently miss a concurrent commit: the writer either blocks, or fails with `SQLITE_BUSY`/`BUSY_SNAPSHOT`. A fresh connection to an in-memory test database would be a different, empty database.
  - The reason: MySQL request sessions are REPEATABLE READ and may have taken their snapshot before the lock, so an in-session read could miss a value that the previous lock holder committed.
  - Values reserved in the same request are still checked in memory.
- **Deadlock note**: a single-user modify that has already flushed its user row can deadlock with a bulk activation that holds the lock and waits on `FOR UPDATE` of that row. The database aborts one side, which stores nothing. `acquire_named_lock` runs under `no_autoflush` to keep that window small. The case is listed in the spec's edge cases.
- **Where it is taken**: once per transaction, in `enforce_unique_proxy_secrets` and `issue_unique_secrets`. Idempotent per session: the session's `info` dict records the acquisition, keyed by transaction.
- **Coverage**:
  - Create, bulk create, modify and revoke, through `prepare_fork_proxy_settings`.
  - Entitled grant, bulk activate and core grant, through `issue_unique_secrets`.
  - Repair.
- **Lock timeout**: A lock-wait timeout (MySQL 1205, PG `lock_not_available`/55P03, SQLite "database is locked") is mapped to HTTP 503 in `UserOperation._prepare_user_proxy_settings` (app/operation/user.py:449-475, next to the existing 409 mapping) and in the fork bulk-activation operations (user_extras.py). Group and core paths keep their existing mapping.
- **Missing row**: `acquire_named_lock` tries `UPDATE` first. If `rowcount == 0`, it runs the dialect insert-ignore (the pattern at crud/wireguard.py:423-440) and then `UPDATE` again. Tests built with `metadata.create_all` therefore work without a seed. A missing table is a hard database error: the write fails and nothing is stored.
- **Rejected**:
  - `GET_LOCK`: it is session-scoped, and the async pool can release it on another connection.
  - `pg_advisory_xact_lock`: it covers PostgreSQL only.
  - A unique index or generated column on users: it rebuilds the users table (see the mysql-fk-migration-oom memory note).

### D-E4b L2TP bulk + MTProto uniqueness (P2)
- **L2TP bulk**: `bulk_activate_l2tp_passwords` in user_extras.py:54-97 becomes `return await self._bulk_activate_entitled_secret(db, bulk_model, EntitledSecretField.l2tp_password)`. The old read–generate–replace code is deleted, and so is `get_users_for_l2tp_activation` once nothing references it.
- **MTProto on create and modify**:
  - `mtproto.secret` joins the guarded set, either in fields.py or as a separate MTProto spec that `enforce_unique_proxy_secrets` checks.
  - It gets `SecretFieldSpec(ProxySecretField.mtproto_secret, "mtproto", "secret", <32-hex generator equal to generate_mtproto_secret>)` in fields.py.
  - A colliding value, supplied or generated, is replaced by a fresh unique one with a WARNING. This is the rule the 8 guarded fields already follow (uniqueness.py:132-148).
  - After 5 failed attempts, `ProxySecretUniquenessError` is raised and mapped to 409 (operation/user.py:472).
  - The reason not to refuse: the reseller bot sends template values, as it already does for WireGuard.
  - Format validation stays in `prepare_mtproto_secret`.

### D-E6 Residuals (P2)
- **(a) Next plan.** `reset_user_by_next` calls `grant_entitled_secrets_after_plan_change(db, db_user)` (new, in app/fork/proxy_secrets/entitled.py) immediately after `sync_user_allocations` at :1357.
  - The helper calls `grant_entitled_secrets(db, [db_user])`.
  - It catches `ProxySecretUniquenessError`, logs at ERROR with the user id and field, and returns. The plan still applies.
  - The import is local, to avoid a cycle.
- **(b) MTProto 409.** The fork link handler records, per render, whether an MTProto link was skipped for a missing secret. When the final links output is empty and that flag is set, it raises `UnissuedSecretError("MTProto")`, which is the 409.
  - The hook point is inside app/fork/subscription/links.py and helpers.py.
  - If `StandardLinks.render` offers no seam, P2 adds one minimal call in app/subscription/links.py and lists that file in OVERRIDES.md.

### D-D5 Deleted-nodes series (P4)
- **Pure helper**: dashboard/src/fork/charts/all-nodes-series.ts exports `buildAllNodesSeries(statsByNode, nodeList, labelFor)`, which returns `{ rows, seriesKeys, totalBytes, perNodeBreakdownUnavailable, pieSlices }`.
- **D4 branch**:
  - When only `'-1'` is present, `rows` is `[]`, `perNodeBreakdownUnavailable` is `true` and the total is the measured sum. This is the D4 branch, moved as it is.
  - Key `'0'`, when its sum is > 0, becomes the series `__deleted_nodes__`. It is stacked last, and its colour is `hsl(var(--muted-foreground))`.
  - `totalBytes` is the sum over the drawn series only, so the header equals bars equals pie.
- **Component edit**: all-nodes-stacked-bar-chart.tsx replaces its inline memo with the helper and renders the extra series in bars, area, legend and pie. The label is `t('statistics.deletedNodes')`.
- **Locale keys**: added to dashboard/src/fork/locales/{en,fa,ru,zh}.json:
  - en "Deleted nodes"
  - fa "نودهای حذف‌شده"
  - ru "Удалённые узлы"
  - zh "已删除的节点"
- **Tests**:
  - dashboard/src/fork/charts/all-nodes-series.test.ts uses `bun:test` and pins D4 and D5.
  - dashboard/package.json gets `"test": "bun test src"`.
  - dashboard-smoke.yml gets a `bun run test` step after `bun install`.

### D-G2 Host ops (main session at deploy; never committed)
**Where**: on the production panel host, during the v5.34.25 deploy window.

**Changes**:
1. Back up `/opt/pasarguard/docker-compose.override.yml` and `/opt/pasarguard/.env` to `*.bak-v53425`.
2. `mkdir -p /var/lib/pasarguard/logs`. This is inside the mounted volume.
3. Add to the override file, under the panel service: `logging: {driver: json-file, options: {max-size: "100m", max-file: "5"}}`.
4. In `.env`, set `SAVE_LOGS_TO_FILE=true` and `LOG_FILE_PATH=/var/lib/pasarguard/logs/panel.log`. Keep the default RotatingFileHandler of 10 MiB × 72. Do not enable `LOG_ROTATION_ENABLED`.

**Verification**:
- `docker compose config` must show the logging block.
- After recreate, `docker inspect` must show `LogConfig` with max-size and max-file.
- The log file must grow.
- A second recreate must keep the earlier lines.

**Rollback**: restore both backups, then recreate.

**Cap**: about 500 MB of Docker logs plus about 730 MB of file log; disk is 25 GB free. The panel runs a single process, so the RotatingFileHandler has no multi-writer race.

## Project Structure

### Documentation (this feature)
```text
specs/003-close-open-audit-items/
├── spec.md
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/behaviour.md
├── checklists/requirements.md
└── tasks.md
```

### Owners and exclusive files

**P1 recorder/scheduler** (A9, A6, A2-r):
- `app/jobs/record_usages.py`
- `app/scheduler.py`
- `app/fork/jobs/usage_limits.py` (new)
- `tests/test_recorder_reset_reads.py`
- `tests/test_recorder_chart_fence.py`
- `tests/test_record_usages.py`
- `tests/test_record_usages_volatile_window.py`
- `tests/test_recorder_harness.py`
- `tests/test_usage_same_tick_limit.py` (new)
- `tests/test_recorder_atomic_cohort.py` (new)
- `tests/test_scheduler_misfire_grace.py` (new)

**P2 secrets/WireGuard** (E1, E3, E4b, E6):
- `app/db/crud/wireguard.py`
- `app/fork/proxy_secrets/uniqueness.py`, `entitled.py`, `fields.py`, `repair.py`, `schemas.py`
- `app/fork/proxy_secrets/locks.py` (new)
- `app/fork/proxy_secrets/wireguard_scope.py` (new)
- `app/fork/models/named_lock.py` (new)
- `app/fork/models/__init__.py`
- `app/db/migrations/versions/<rev>_named_locks.py` (new)
- `app/fork/operation/user_extras.py`
- `app/fork/operation/entitled_secrets.py`
- `app/fork/crud/bulk.py`
- `app/utils/mtproto.py`
- `app/operation/user.py` (503 mapping only)
- `app/db/crud/user.py` (one call in `reset_user_by_next`)
- `app/fork/subscription/helpers.py`
- `app/fork/subscription/links.py`
- `app/subscription/links.py` (only if no seam exists)
- `tests/test_bulk_l2tp_activation.py`
- `tests/test_wireguard_reconcile_shared_keys.py` (new)
- `tests/test_wireguard_client_keys_scope.py` (new)
- `tests/test_proxy_secret_lock.py` (new)
- `tests/test_mtproto_secret_uniqueness.py` (new)
- `tests/test_next_plan_entitled_grant.py` (new)
- `tests/test_mtproto_links_409.py` (new)
- `tests/test_named_lock_migration.py` (new)

**P3 node/admin/tests/ledger** (A3, reset, G5, B1, ledger):
- `app/node/__init__.py`
- `app/db/crud/admin.py`
- `OVERRIDES.md`
- `tests/test_node_update_in_place.py` (new)
- `tests/test_reset_admin_usage_relative.py` (new)
- `tests/api/test_traffic_log_purge_http.py` (new)
- `tests/test_set_owner_relative_update.py` (new)

**P4 dashboard** (D5, D4 pin):
- `dashboard/src/components/charts/all-nodes-stacked-bar-chart.tsx`
- `dashboard/src/fork/charts/all-nodes-series.ts` (new)
- `dashboard/src/fork/charts/all-nodes-series.test.ts` (new)
- `dashboard/src/fork/locales/{en,fa,ru,zh}.json`
- `dashboard/package.json` (scripts only)
- `.github/workflows/dashboard-smoke.yml`

Shared-file rule: every file has exactly one owner.
- Other owners send their OVERRIDES.md lines to P3 in their hand-off.
- P3's B1 test reads app/db/crud/user.py but does not edit it.
- P1 imports `_review_user_select_stmt` and `apply_status_changes` without editing their files.
- Nobody but P2 edits app/db/crud/user.py.

## Complexity Tracking

| Deviation | Why needed | Simpler alternative rejected because |
|---|---|---|
| Edits inside upstream record_usages.py, scheduler.py, node/__init__.py, crud/admin.py, crud/user.py (1 line), crud/wireguard.py, operation/user.py (error mapping) | The defects are inside upstream functions and no seam intercepts them | Wrapping the recorder's transaction from outside would duplicate its retry and fence logic |
| New fork table `named_locks` + migration | Portable, transaction-scoped serialization on all 5 CI backends | GET_LOCK leaks with pooled async sessions; pg-only advisory locks do not cover MySQL; a users-table unique index needs a table rebuild |
| Fresh READ COMMITTED connection for holder lookups | MySQL REPEATABLE READ snapshot can predate the lock | Locking reads (FOR SHARE) on a JSON predicate would lock every scanned users row and block the recorder |
