# Upstream Override Ledger

Every file in this fork that **removes or changes upstream lines** (category `override`)
must be listed here with a one-line justification. The CI workflow
upstream-inventory.yml fails the PR if an override appears in a file not listed below.

Generated from `scripts/upstream_inventory.py --base upstream/main --head HEAD`
(upstream/main @ `aebf7256`, v5.3.0). Measured: 233 diverged files — 111 fork-only,
39 pure-addition, 72 override, 11 mechanical — 443 override lines total.

The checker reads backticked file paths from this file; keep one `` `path` `` per entry.

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
- `dashboard/src/pages/_dashboard.bulk.tsx` — bulk page icon set
- `dashboard/src/pages/_dashboard.settings.tsx` — settings page icon set
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

## Build & packaging

- `build_dashboard.sh` — dashboard build command change
- `dashboard/package.json` — @pasarguard/core-kit version bump
- `dashboard/vite.config.mts` — emptyOutDir enabled to stop chunk pile-up
