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
- `app/db/crud/admin.py`
- `app/db/crud/general.py`
- `app/lifecycle.py`
- `app/nats/router.py`
- `app/telegram/fsm_storage.py`
- `app/telegram/handlers/admin/bulk_actions.py`
- `app/utils/crypto.py`
- `app/utils/jwt.py`
- `app/jobs/record_usages.py` — except-fix plus NodeUsage/NodeUserUsage import update
- `app/scheduler.py` — job_defaults max_instances 30 -> 1 with coalesce, so a job that does not set it cannot overlap itself
- `app/subscription/share.py` — except-fixes plus WireGuardConfiguration union update
- `app/operation/subscription.py` — except-fixes, announce payload formatting, plus subscription access-log recording on every config-revealing path

## connect_node multicore generalization (upstream PR candidate)

- `app/operation/node.py` — connect_node generalized across cores with bounded concurrency; the local log-stream getter also returns the fork's fork_log_stream seam so the traffic-log collector stays the only reader of the node's shared log channel; attaching to an already-running core now re-pushes the user list, because without it a core that came up without users rejects every client forever
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

- `app/db/crud/user.py` — chunked bulk user queries and owner operations
- `app/db/crud/bulk.py` — bulk target filter refactor (_create_final_filter)
- `app/operation/user.py` — wireguard key preparation and validated-user flow
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

## Tests

- `tests/api/__init__.py` — test app import cleanup
- `tests/api/test_usage_functions_timezone.py` — admins parameter updates
- `tests/api/test_user.py` — crud import updates and flow assertion
- `tests/api/test_host.py` — settings assertion update
- `tests/api/test_node.py` — serialize lambda signature update
- `tests/test_review_users_unit.py` — import block re-sorted for the fork's combine-as-imports ruff config

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
- `app/node/__init__.py` — get_healthy_nodes reports which nodes it is skipping instead of dropping them from a list comprehension in silence; an unhealthy node produces no usage rows at all, and before this there was no log line anywhere to say so while the stored status still read connected
- `app/jobs/node_checker.py` — each node's health check is bounded by NODE_CHECK_TIMEOUT, so a node that never answers cannot leave the gathered job running forever and make the scheduler skip every later round (the panel was observed logging `maximum number of running instances reached` every ten seconds, which left the in-memory health stale and silently removed nodes from usage collection)
- `app/jobs/send_notifications.py` — 2 lines, reason not recorded
- `app/node/worker.py` — 2 lines, the NATS log relay takes its per-node stream from the fork's fork_log_stream seam so the traffic-log collector stays the only reader of the node's shared log channel
- `app/operation/core.py` — 1 line, reason not recorded
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
- `tests/test_record_usages.py` — 2 lines, reason not recorded
