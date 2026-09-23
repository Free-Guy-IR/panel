# Upstream Override Ledger

Every file in this fork that **removes or changes upstream lines** (category `override`)
must be listed here with a one-line justification. The CI workflow
upstream-inventory.yml fails the PR if an override appears in a file not listed below.

Generated from `scripts/upstream_inventory.py --base v5.4.1 --head HEAD`, where the
baseline is the upstream tag pinned in the .upstream-baseline file (v5.4.1 @ `b56ffe36`).
Current measurement: 460 diverged files — 299 fork-only, 36 pure-addition, 109 override,
16 mechanical — 1397 override lines, and 4460 fork lines still living inside
upstream-tracked files.

The extraction moved 2102 fork lines out of upstream-tracked files (5246 to 3144) and
restored the four `dashboard/public/statics/locales/*.json` files to upstream byte-for-byte,
which alone removed 100 override lines. Override lines nevertheless rose by 206, and 186 of
that comes from a single file, `dashboard/src/features/hosts/dialogs/host-modal.tsx`: its
WireGuard host layout is upstream code that now lives in `dashboard/src/fork/hosts/`, so the
upstream-shaped file deletes 191 upstream lines. Nothing is lost at runtime, but that file is
now the largest single merge liability in the fork. The preferred shape is the one used for
Outline: keep the upstream file pristine and express fork behaviour as a thin subclass or
an additive section, rather than deleting the upstream implementation.

The checker reads backticked file paths from this file; keep one `` `path` `` per entry.

The gate measures divergence against the tag named in the .upstream-baseline file, not against
`upstream/main`. That is deliberate: `upstream/main` moves every time PasarGuard publishes a
release, and when v5.4.0 and v5.4.1 landed the gate went red with 76 "override" files that were
simply upstream's own new migrations, jobs and NATS modules, none of which this fork has yet.
A gate that turns red without anyone touching the fork is a gate people learn to ignore.
Bumping that pin is therefore part of merging upstream, not a chore to do separately:
merge the release, re-run the inventory, update the numbers above and the entries below, then move
the pin. The scheduled report still runs against `upstream/main`, so how far behind the fork is
stays visible.

## Branding & fork infrastructure (not upstreamable)

- `app/db/migrations/env.py` — imports `app/db/models.py` so the fork model modules register their tables on the shared declarative metadata before autogenerate reads it, and adds `MIGRATION_OWNED_TABLES` with an `_include_name` hook handed to both of alembic's context.configure calls; a table that a hand-written migration owns and deliberately has no ORM model for (`content_filter_sniffing_repairs`) is therefore skipped by autogenerate instead of being emitted as a drop. Because the hook sits on the offline and the online path alike, it changes autogenerate behaviour for the whole fork, not just for that one table
- `README.md` — fork branding: logo, badges, links point to Free-Guy-IR
- `README-fa.md` — fork branding, Persian edition
- `README-ru.md` — fork branding, Russian edition
- `README-zh-cn.md` — fork branding, Chinese edition
- `dashboard/src/components/layout/footer.tsx` — footer branding and repo link
- `docker-compose.yml` — fork container image ghcr.io/free-guy-ir/panel

## Python `except (A, B)` syntax fixes (upstream PR candidates)

Upstream uses the Python-2-style `except A, B:` which is a SyntaxError on Python 3;
the fork parenthesizes every occurrence. Each file below is that fix unless noted.

- `main.py`
- `app/core/manager.py`
- `app/core/xray.py`
- `app/db/crud/admin.py` — except-fix; plus `reset_admin_usage` subtracts the logged value with a relative SQL update (`used_traffic = used_traffic - logged`) instead of assigning 0, so bytes a usage tick bills between the admin's read and the reset commit stay on the counter while the usage log still records the pre-reset value (v5.34.25); `reset_admin_usage` also locks the admin row, logs `min(read, locked)` and reactivates from the locked status, and `update_admin` accepts a pre-computed `hashed_password` so the caller can hash outside the row lock (v5.34.25, a2-usage-3)
- `app/db/crud/general.py`
- `app/lifecycle.py`
- `app/nats/router.py`
- `app/telegram/fsm_storage.py`
- `app/telegram/handlers/admin/bulk_actions.py`
- `app/utils/crypto.py`
- `app/utils/jwt.py`
- `app/jobs/record_usages.py` — except-fix plus NodeUsage/NodeUserUsage import update; since v5.34.25 (A2-r) a cohort's chart write, billing and admin update run in one retried transaction, with the fence filtering the chart rows, so a failed cohort rolls back whole and is safe to retry, and (A9) the billed user ids go to the fork's `enforce_user_limits_now`, so a user who crosses the limit is limited in the same tick
- `app/scheduler.py` — job_defaults max_instances 30 -> 1 with coalesce, so a job that does not set it cannot overlap itself; since v5.34.25 (A6) also misfire_grace_time 30, so a job that starts up to 30 s late still runs instead of being skipped
- `app/subscription/share.py` — except-fixes plus WireGuardConfiguration union update
- `app/operation/subscription.py` — except-fixes, announce payload formatting, plus subscription access-log recording on every config-revealing path

## connect_node multicore generalization (upstream PR candidate)

- `app/operation/node.py` — connect_node generalized across cores with bounded concurrency; the local log-stream getter also returns the fork's fork_log_stream seam so the traffic-log collector stays the only reader of the node's shared log channel; attaching to an already-running core now re-pushes the user list, because without it a core that came up without users rejects every client forever; `modify_node` forces a Start only when the stored `core_config_id`, additional cores or `keep_alive` actually changed, where upstream forced one on every edit and the fork previously forced one whenever the fields were merely present in the body, which the dashboard sends on every save, so renaming a node or changing its coefficient stopped and restarted its core (v5.34.25, A3)
- `app/db/crud/node.py` — core_config_id-aware node filtering, and the per-node resource-history query compares its HAVING clause against the `period_start` alias the way the usage query already did; re-emitting the truncation expression made MySQL reject the whole statement with `Unknown column 'node_stats.created_at' in 'having clause'`, which is why that endpoint answered 503 on every node
- `app/node/manager_sync.py` — call-site update for new connect_node signature
- `app/node/sync.py` — _serialize_user_for_node signature update
- `app/node/user.py` — node user serialization carries vless id
- `tests/test_connect_concurrency.py` — test updated to new connect_node signature
- `tests/test_node_start_timeout.py` — the attach path now refreshes the user list, so the doubles carry sync_users and an id

## New proxy cores: openvpn / l2tp / mtproto / singbox

- `app/core/hosts.py` — openvpn/l2tp subscription inbound data plus except-fixes
- `app/models/host.py` — host override models reshuffled for new cores
- `app/models/user.py` — proxy_settings import and field update
- `app/db/crud/core.py` — CoreConfig/Node import update
- `dashboard/src/features/hosts/dialogs/host-modal.tsx` — host mode resolution for new protocols
- `dashboard/src/features/nodes/dialogs/core-config-modal.tsx` — config modal beyond xray/wg
- `dashboard/src/features/nodes/dialogs/node-modal.tsx` — node form fields for new cores
- `dashboard/src/features/nodes/forms/core-config-form.ts` — backend types mtproto/singbox
- `dashboard/src/features/nodes/components/cores/cores-list.tsx` — cores list modal behavior

## Dashboard core-editor multi-core support

- `dashboard/src/features/core-editor/routes/core-editor-page.tsx` — editor page handles multiple core kinds
- `dashboard/src/features/core-editor/state/core-editor-store.ts` — store keyed by core kind
- `dashboard/src/features/core-editor/state/use-core-draft-sync.ts` — draft sync signature per kind
- `dashboard/src/features/core-editor/kit/core-kind.ts` — core kind guard extended
- `dashboard/src/features/core-editor/kit/core-section-nav.ts` — section nav per core kind
- `dashboard/src/features/core-editor/components/shell/core-section-sidebar.tsx` — sidebar sections per kind
- `dashboard/src/features/core-editor/components/shared/core-command-menu.tsx` — command menu sections per kind
- `dashboard/src/features/core-editor/components/shared/validation-summary.tsx` — validation item sources
- `dashboard/src/features/core-editor/components/xray/xray-inbounds-section.tsx` — xhttp padding default handling
- `dashboard/src/features/core-editor/components/xray/xray-advanced-section.tsx` — advanced tabs per kind

## Dashboard UI & features

- `dashboard/src/pages/_dashboard.statistics.tsx` — statistics page layout rework, plus the fork statistics-view seam and the owner-only gate on the fleet-wide views
- `dashboard/src/pages/_dashboard.tsx` — 2 lines, the upstream donation popup is not shown to this fork's operators
- `dashboard/src/features/statistics/components/system-statistics-section.tsx` — Mbps/MB formatting change
- `dashboard/src/features/bulk/components/bulk-flow.tsx` — bulk flow operations UI
- `dashboard/src/features/users/components/action-buttons.tsx` — protocol icons and download types
- `dashboard/src/features/users/dialogs/user-hwids-modal.tsx` — hwid query invalidation
- `dashboard/src/utils/subscription-config.ts` — subscription content format types
- `dashboard/src/locales/i18n.ts` — locale loadPath under base URL

## Backend operations & CRUD

- `app/db/crud/user.py` — chunked bulk user queries and owner operations; plus one local-import call in `reset_user_by_next` to `grant_entitled_secrets_after_plan_change`, right after `sync_user_allocations`, so a next plan grants the secrets its groups entitle (v5.34.25, E6a)
- `app/db/crud/bulk.py` — bulk target filter refactor (_create_final_filter)
- `app/operation/user.py` — wireguard key preparation and validated-user flow; plus a `DBAPIError` branch in `_prepare_user_proxy_settings` that maps a proxy-secrets lock-wait timeout to HTTP 503 via `is_lock_wait_timeout` (v5.34.25, E3)
- `app/operation/group.py` — `_sync_users_allocations` (the second step of every group path: WireGuard allocation plus the entitled-secret grant) runs through the fork module app/fork/proxy_secrets/group_repair.py. It runs in a savepoint and is retried once after a short pause when the proxy secrets lock is unavailable (deleted `named_locks` row, deadlock, lock-wait timeout). If it still fails, the request rolls back and answers 503 (the upstream crud has already saved the group change), and a background repair re-runs the step for exactly those user ids at 5 s, 30 s and 2 min (v5.34.25, E3)
- `app/operation/__init__.py` — scope_action parameter plumbing
- `app/operation/hwid.py` — get_validated_user_by_id flow
- `app/middlewares/__init__.py` — settings import update
- `app/routers/core.py` — fastapi import update
- `app/routers/group.py` — require_permission import update
- `app/routers/subscription.py` — 5 lines; the public /sub routes pass the caller's user-agent and IP down to the subscription access log so every path that hands out a config leaves a delivery record. The user-agent is read off the request headers rather than through a new FastAPI Header parameter, so no route signature and no OpenAPI shape changes; the five lines are single-line calls reflowed to take the extra keyword arguments
- `app/templates/__init__.py` — jinja environment setup update
- `app/subscription/xray.py` — finalmask stream settings emission
- `app/db/compiles_types.py` — registers the MySQL compilation of `CaseSensitiveString`, `DaysDiff` and `DateDiff` for the `mariadb` dialect name as well. SQLAlchemy gives a `mariadb://` URL its own dialect name, which upstream's `mysql`-only hooks never match, so on MariaDB the collation was dropped silently and the two date helpers raised `UnsupportedCompilationError` in the `days_left` expression and the usage-reset queries. `config.py` accepts those URLs, so this is a supported configuration the fork has to make work
- `app/db/crud/settings.py` — 1 line; `get_settings` is annotated `Settings | None`, which is what `scalar_one_or_none` has always returned. The upstream annotation promised a row that may not exist and every caller believed it
- `app/settings/__init__.py` — the seven settings accessors read their section through one `stored_settings` helper that raises `SettingsRowMissing` naming migration `9af04c077ede`, instead of each dereferencing a possibly-absent row and dying at startup with a bare `AttributeError` on `NoneType`
- `app/jobs/review_users.py` — limit_users_job hands its candidate ids to the fork's row-locked limit_users_under_lock, so the usage tick and the review job never apply a transition twice
- `app/operation/admin_sync.py` — `limit_exceeded_admins` limits each candidate through the fork's `limit_admin_if_over_quota` (row lock with the over-quota predicate plus a conditional UPDATE) under the shared `admin_transition_lock`, instead of writing `status=limited` onto an ORM object loaded before the loop, so a quota raised or reset mid-loop is never overwritten and the review job and the recorder never flip the same admin twice (v5.34.25, a2-usage-3)
- `app/operation/admin.py` — modify and reset (single and bulk) take `admin_transition_lock`, lock the admin row, and decide the old user-sync state from the locked status, so a job limit that lands between the read and the write cannot leave a raised or reset admin limited with its users cut; the password is hashed before the row lock (v5.34.25, a2-usage-3)

## Tests

- `tests/api/__init__.py` — test app import cleanup
- `tests/api/test_usage_functions_timezone.py` — admins parameter updates
- `tests/api/test_user.py` — crud import updates and flow assertion
- `tests/api/test_host.py` — settings assertion update
- `tests/api/test_node.py` — serialize lambda signature update
- `tests/test_review_users_unit.py` — import block re-sorted for the fork's combine-as-imports ruff config
- `tests/test_node_manager.py` — upstream's `test_update_node_replaces_on_name_or_coefficient_change` asserted that a name or usage-coefficient change rebuilds the bridge, which sends the node a Stop; since v5.34.25 (A3) the live bridge is updated in place, so the test asserts the same object with the new name and coefficient and no Stop, and a rebuild with a Stop only when the connection itself changes

## Overrides the ledger gate caught unlisted (v5.34.24 audit)

These eight files already failed the ledger gate at `72ad0e4a5`; each carried a fork change
without an entry here.

- `app/app_factory.py` — 1 upstream line; the shutdown hook awaits `drain_usage_jobs` before `stop_job_leader`, so a usage tick that has already reset the node counters finishes billing them instead of being dropped mid-write on a redeploy
- `app/db/crud/wireguard.py` — subnet reconcile passes `shared_wireguard_public_keys()` for each loaded chunk into `_ensure_wireguard_keys()`, so WireGuard-bound users that share a key get a fresh unique pair (v5.34.25, E1); the fork's pool-backed allocation and reconcile also live in this upstream file
- `app/operation/system.py` — 1 upstream line; for a non-owner admin the users-stats response carries that admin's coefficient-adjusted usage as `admin_used_traffic` and leaves `outgoing_bandwidth` at 0, instead of passing accounted usage off as raw downlink bandwidth
- `app/telegram/utils/texts.py` — 1 upstream line; the bot's start message shows `admin_used_traffic`, when present, as "Accounted Usage (coefficient-adjusted)" instead of summing it into "Total Data Usage" as if it were raw bandwidth
- `dashboard/src/components/charts/all-nodes-stacked-bar-chart.tsx` — D4: when the backend returns only the aggregate series (key `'-1'`), the chart shows a "per-node breakdown unavailable" state with the measured total instead of dividing the aggregate evenly across the nodes, which drew per-node numbers nobody measured. D5 (v5.34.25): the series building moved to `buildAllNodesSeries` in the fork module dashboard/src/fork/charts/all-nodes-series.ts, and usage of deleted nodes (key `'0'`) is drawn as a "Deleted nodes" series stacked last and as the last pie slice, so the header total equals the sum of what is drawn
- `dashboard/src/features/dashboard/components/dashboard-statistics.tsx` — when the stats response carries `admin_used_traffic`, the traffic card labels it "Accounted usage" and hides the incoming/outgoing split, which does not exist for that figure; most of the 14 upstream lines are the split block re-indented under the condition
- `dashboard/src/features/users/dialogs/usage-modal.tsx` — D4 in the per-user usage modal: an aggregate-only response shows the "per-node breakdown unavailable" state and the measured total instead of an even split across nodes, so the memo returns the chart rows together with that flag and the aggregate total
- `tests/api/conftest.py` — 2 upstream lines; the autouse fixture also points the `GetDB` that app/utils/jwt.py imports at the test database, because the JWT secret is read from the database and the tests must sign and verify with the test database's key; the package import is the absolute `from tests.api import ...` form instead of upstream's relative one, with no behaviour change

## Build & packaging

- `build_dashboard.sh` — dashboard build command change
- `dashboard/package.json` — @pasarguard/core-kit version bump
- `dashboard/vite.config.mts` — emptyOutDir enabled to stop chunk pile-up

## Fork boundary seam (minimum cost of the extraction)

These three files are the only upstream-shaped files the boundary extraction turned from
`pure-addition` into `override`. Each one is a hook point, not a behaviour change.

- `app/routers/__init__.py` — replaces the eager five-line `api_router` assembly with a locked lazy builder that registers the fork routers before the upstream ones and refuses to publish a partially built router (5 upstream lines)
- `app/subscription/__init__.py` — resolves `OutlineConfiguration` from the fork subscription package so the fork subclass is the active format; the Outline subscription module itself is back to upstream byte-for-byte (1 upstream line)
- `dashboard/src/features/core-editor/kit/core-editor-change-state.ts` — delegates the fork core kinds to `@/fork/stores/change-state` (2 upstream lines)

## Undocumented overrides inherited from before the extraction (pending audit)

The ledger gate was already failing on these 27 files before the boundary work started;
they carry 201 override lines between them. They are listed here so the gate reflects the
real boundary, and each one still needs its own justification or a revert.

- `dashboard/src/features/nodes/components/cores/logs.tsx` — 121 upstream lines removed, the largest inherited override; the upstream WebSocket log viewer was replaced wholesale and the reason is not recorded
- `dashboard/src/features/admins/components/admins-table.tsx` — 14 lines; upstream's `dangerouslySetInnerHTML` confirm prompts were replaced with escaped rendering
- `app/db/models.py` — 14 lines; the ORM cascade relationships on the node usage tables were dropped because cascading deletes deadlock on MySQL at this fleet size, and those `node_id` foreign keys became nullable `ondelete="SET NULL"` so deleting a node no longer walks its usage rows. The file now also ends with `_register_fork_model_tables`, which imports the fork model modules so their tables join the shared declarative metadata, and `fk_id_column` forwards a `name` keyword through to `ForeignKey` so a fork table can pin a constraint name short enough for PostgreSQL's 63-character identifier limit
- `app/operation/permissions.py` — 4 lines, reason not recorded
- `app/subscription/base.py` — 5 lines, reason not recorded
- `app/notification/webhook/__init__.py` — 1 line, reason not recorded
- `app/node/__init__.py` — get_healthy_nodes reports which nodes it is skipping instead of dropping them from a list comprehension in silence; an unhealthy node produces no usage rows at all, and before this there was no log line anywhere to say so while the stored status still read connected. `update_node` also updates a live bridge's `name` and `usage_coefficient` in place when the connection signature is unchanged, where upstream rebuilt the bridge and sent the node a Stop for a rename, and the signature treats a blank and a null `proxy_url` as the same connection, because the API stores `''` on create while the dashboard sends `null` on edit, which made the first rename of every proxy-less node a rebuild (v5.34.25, A3)
- `app/jobs/node_checker.py` — each node's health check is bounded by NODE_CHECK_TIMEOUT, so a node that never answers cannot leave the gathered job running forever and make the scheduler skip every later round (the panel was observed logging `maximum number of running instances reached` every ten seconds, which left the in-memory health stale and silently removed nodes from usage collection)
- `app/jobs/send_notifications.py` — 2 lines, reason not recorded
- `app/node/worker.py` — 2 lines, the NATS log relay takes its per-node stream from the fork's fork_log_stream seam so the traffic-log collector stays the only reader of the node's shared log channel
- `app/operation/core.py` — 1 line, reason not recorded; plus one `grant_entitled_secrets_for_core` call after `core_manager.update_core` in `create_core` and `modify_core`, so a core that starts serving an OpenVPN, L2TP or MTProto inbound some groups already carry issues the missing credential to every entitled user instead of leaving them with an empty profile
- `app/routers/admin.py` — 2 lines, reason not recorded
- `app/routers/node.py` — the fleet-wide realtime statistics endpoint is restricted to the panel owner at the operator's request, and the node log stream is too: those lines carry every client address and destination, which is the same data the owner-only traffic log exists to protect
- `config.py` — the four TRAFFIC_LOG_* job settings are added to JobSettings alongside the fork's existing node_user_usages_retention_days; and the node resource-sample recorder is only forced off on SQLite now rather than on everything except PostgreSQL, because the read side already speaks all three dialects, which is why node_stats had never held a single row on this MySQL panel
- `dashboard/src/components/layout/sidebar.tsx` — 1 line, fork main-nav items are taken from the owner-aware accessor so owner-only entries are never rendered for other admins
- `dashboard/src/components/layout/tabbed-route-suspense-fallback.tsx` — the settings loading skeleton drops fork tabs the current admin may not open
- `dashboard/src/pages/_dashboard.settings.tsx` — the settings tab list drops fork tabs the current admin may not open
- `app/routers/system.py` — 1 line, reason not recorded
- `app/subscription/clash.py` — 2 lines, reason not recorded
- `app/subscription/links.py` — 1 line, reason not recorded
- `app/subscription/singbox.py` — 2 lines, reason not recorded
- `app/templates/subscription/index.html` — 1 line, reason not recorded
- `dashboard/src/features/admin-roles/components/admin-role-actions-menu.tsx` — 2 lines, reason not recorded
- `dashboard/src/features/groups/components/group-actions-menu.tsx` — 2 lines, reason not recorded
- `dashboard/src/features/groups/components/group.tsx` — 2 lines, reason not recorded
- `dashboard/src/features/hosts/components/host-actions-menu.tsx` — 2 lines, reason not recorded
- `dashboard/src/features/nodes/components/node-actions-menu.tsx` — 3 lines, reason not recorded
- `dashboard/src/features/templates/components/client-template-actions-menu.tsx` — 10 lines, reason not recorded
- `dashboard/src/features/templates/components/user-template-actions-menu.tsx` — 2 lines, reason not recorded
- `dashboard/src/pages/_dashboard.nodes.cores._index.tsx` — 2 lines, reason not recorded
- `tests/api/test_bulk_entity_actions.py` — 1 line, reason not recorded
- `tests/api/test_core.py` — 1 line, reason not recorded
- `tests/test_record_usages.py` — the session fixture also patches GetDB in app/fork/jobs/usage_limits.py so the same-tick limit reads the test database, the two chart-writer tests are rewritten for the single-transaction cohort write, and the scheduler-defaults pin also asserts misfire_grace_time 30 (v5.34.25)
