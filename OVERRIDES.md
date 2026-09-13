# Upstream Override Ledger

Every file in this fork that **removes or changes upstream lines** (category `override`)
must be listed here with a one-line justification. The CI workflow
upstream-inventory.yml fails the PR if an override appears in a file not listed below.

Generated from `scripts/upstream_inventory.py --base v5.4.1 --head HEAD`, where the
baseline is the upstream tag pinned in the .upstream-baseline file (v5.4.1 @ `b56ffe36`).
Current measurement: 358 diverged files — 205 fork-only, 37 pure-addition, 101 override,
15 mechanical — 1227 override lines, and 3311 fork lines still living inside
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
- `app/subscription/share.py` — except-fixes plus WireGuardConfiguration union update
- `app/operation/subscription.py` — except-fixes plus announce payload formatting

## connect_node multicore generalization (upstream PR candidate)

- `app/operation/node.py` — connect_node generalized across cores with bounded concurrency
- `app/db/crud/node.py` — core_config_id-aware node filtering
- `app/node/manager_sync.py` — call-site update for new connect_node signature
- `app/node/sync.py` — _serialize_user_for_node signature update
- `app/node/user.py` — node user serialization carries vless id
- `tests/test_connect_concurrency.py` — test updated to new connect_node signature

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

- `dashboard/src/pages/_dashboard.statistics.tsx` — statistics page layout rework
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
- `app/templates/__init__.py` — jinja environment setup update
- `app/subscription/xray.py` — finalmask stream settings emission

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
- `app/db/models.py` — 13 lines; the ORM cascade relationships on the node usage tables were dropped because cascading deletes deadlock on MySQL at this fleet size
- `app/operation/permissions.py` — 4 lines, reason not recorded
- `app/subscription/base.py` — 5 lines, reason not recorded
- `app/notification/webhook/__init__.py` — 1 line, reason not recorded
- `app/jobs/node_checker.py` — 1 line, reason not recorded
- `app/jobs/send_notifications.py` — 2 lines, reason not recorded
- `app/node/worker.py` — 2 lines, reason not recorded
- `app/operation/core.py` — 1 line, reason not recorded
- `app/routers/admin.py` — 2 lines, reason not recorded
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
