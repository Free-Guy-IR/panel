# Upstream Sync Ledger

This file records every upstream PasarGuard commit that this fork has not cherry-picked
but has dealt with anyway. The upstream parity gate in `.github/workflows/upstream-inventory.yml`
reads it through `scripts/upstream_inventory.py --fail-on-unported UPSTREAM_SYNC.md` and fails when
an upstream commit past the pinned baseline, on any upstream branch or tag, is neither ported
nor listed here.

A commit ported with `git cherry-pick -x` needs no entry: the `(cherry picked from commit <sha>)`
trailer in the fork history covers it automatically, and so does any rebased or duplicated copy of
a covered commit with the same `git patch-id --stable`.

Everything else goes under one of the two headings below, one list item per commit:

    - `<sha>` <reason>

Use the full 40-character sha. A prefix of at least 9 hex characters is accepted while it names
exactly one upstream commit in range, but it becomes a ledger error once that commit leaves the
range. An entry without a reason fails the gate.

`Ported by design` is for upstream changes the fork carries in a different form (adapted by hand,
merged into a fork commit, or superseded by fork code). `Waived` is for upstream changes the fork
deliberately does not take.

A reason that contains `until: YYYY-MM-DD` covers its commit only up to that date. After it the gate
fails and the commit needs a new decision. Pending upstream feature branches are recorded this way.

A `Waived` reason that starts with `UNMERGED-WIP` or `pending` is also conditional on upstream: it covers
the commit only while upstream keeps it off its release lines (upstream/main, upstream/dev and every
upstream tag). When upstream merges that commit, or a rebased copy of it with the same patch-id, the gate
fails at once, whatever the `until:` date says, and the commit needs a new decision. Any other `Waived`
reason is permanent and also covers rebased copies.

The gate also reads `.upstream-refs`, the snapshot of every upstream branch and tag head the fork
has reviewed. It fails while a live upstream head is missing from the snapshot, and it keeps
tracking the commits of every recorded head after upstream deletes or force-pushes the branch.
After reviewing new upstream heads, record them with
`python scripts/upstream_inventory.py --base <baseline tag> --track 'refs/remotes/upstream/*' --track 'refs/upstream-tags/*' --fail-on-unported UPSTREAM_SYNC.md --refs-snapshot .upstream-refs --write-refs-snapshot`.

## Ported by design

Commits on upstream side branches whose change is already in the fork: either upstream squashed the
branch into a dev commit that is inside the v5.4.1 baseline (DUPLICATE-OF-DEV), or the fork carries
the same change through its own code (ALREADY-IN-FORK). Checked on 2026-09-24 against upstream/dev
ec74c933405c554dccf0dd5b6d0eab05ac5f78cc by comparing the final trees of the touched files.

- `d5e6415451a6363052a892105215fc2243d73734` cherry-picked with -x (36d8a3825) and adapted: the awaited on-hold removal passes the user's hwid_limit like the fork's remove_user (a808adaa3), and tests/api/test_node.py keeps the fork's superset of upstream's record_background_connect double.
- `b606dbc26900873d5a8a88b01fb9f1972b8f3d07` cherry-picked with -x (970b69a74) without its three clear_sub_config_cache() hooks in app/core/hosts.py, app/settings/__init__.py and app/subscription/client_templates.py, which fix owner P10 adds byte-identically on its branch; the fork keeps shared_wireguard_public_keys for subnet reconcile next to upstream's collision repair.
- `98befb12269db8154d9333f29979f8c638376cf7` cherry-picked with -x (d97ebcde0); the fork drops the unused LargeBinary import and unquotes the list["AdminPasskey"] annotation in app/db/models.py for its ruff --no-fix gate (ce9599ea0).
- `889b591d3ed31644a22ce802f7f64fcd7ee81c47` cherry-picked with -x (1f52f1b34); all-nodes-stacked-bar-chart.tsx builds its series through the fork's buildUsageSeries so the D4 aggregate-only state and the D5 deleted-nodes series hold in every breakdown, which replaces upstream's inline series code in that file.
- `be67c4c122956801606c85115c2c91cd2c8a728a` DUPLICATE-OF-DEV: upstream branch feat/on_hold_timeout_action was squashed into dev d5e6415451a6363052a892105215fc2243d73734 (#901), whose tree equals the branch tip for all 10 touched files. The fork already carries that dev commit as 36d8a382532dc4bcad867edf62b389177aa57fd7.
- `1fadd9f55177479211d07645661a28f9299dd178` DUPLICATE-OF-DEV: upstream branch feat/on_hold_timeout_action commit squashed into dev d5e6415451a6363052a892105215fc2243d73734 (dashboard/src/service/api/index.ts blob identical to the branch tip). The fork carries it as 36d8a382532dc4bcad867edf62b389177aa57fd7.
- `516c50b44948520447832e721c27ff4582eb1293` DUPLICATE-OF-DEV: upstream branch feat/on_hold_timeout_action next_plan disabled-status fix squashed into dev d5e6415451a6363052a892105215fc2243d73734 (app/jobs/review_users.py blob identical to the branch tip). The fork carries it as 36d8a382532dc4bcad867edf62b389177aa57fd7.
- `d04afb6dbe4508b1c4aff3d2b68caaa342148ee5` DUPLICATE-OF-DEV: upstream branch feat/on_hold_timeout_action test fix squashed into dev d5e6415451a6363052a892105215fc2243d73734 (tests/api/test_node.py blob identical to the branch tip). The fork carries it as 36d8a382532dc4bcad867edf62b389177aa57fd7.
- `f82c643181b6e0aabe6939daca57364de9abc3a8` DUPLICATE-OF-DEV: upstream branch feat/on_hold_timeout_action fix (do not commit the deletion before node cleanup) squashed into dev d5e6415451a6363052a892105215fc2243d73734 (app/jobs/review_users.py and app/node/sync.py blobs identical to the branch tip). The fork carries it as 36d8a382532dc4bcad867edf62b389177aa57fd7.
- `0707aac33c5f5bca2b8d750664a00778a65fc6b1` DUPLICATE-OF-DEV: upstream branch nodes-shared-storage commit squashed into dev cef3f4e5b164633eb143bf6b6a23df3fff339649 (#746), which is in v5.4.1 and therefore in the fork base. tests/test_nats_leader_heartbeat.py is the same blob on the branch, dev and the fork.
- `0913d7b19bd5033b35c51e13d0c704bf9ff4b69a` DUPLICATE-OF-DEV: upstream branch nodes-shared-storage manager_sync refactor squashed into dev cef3f4e5b164633eb143bf6b6a23df3fff339649 (#746, in v5.4.1). app/node/manager_sync.py at the branch tip equals the squash.
- `0d4fa56e086c0c5e7fae4769c2cc5406ea1fdd28` DUPLICATE-OF-DEV: upstream branch nodes-shared-storage all-in-one role test change squashed into dev cef3f4e5b164633eb143bf6b6a23df3fff339649 (#746, in v5.4.1). The branch-tip workflow and role.py equal the squash.
- `16b33bf77572cc1ee463c22eddd88677941ca653` DUPLICATE-OF-DEV: upstream branch nodes-shared-storage cross-worker node sync squashed into dev cef3f4e5b164633eb143bf6b6a23df3fff339649 (#746, in v5.4.1). app/node/manager_sync.py and tests/test_node_manager_sync.py at the branch tip equal the squash.
- `21328fc95477b428a29ac421b15f9c997fb93e3b` DUPLICATE-OF-DEV: upstream branch nodes-shared-storage nats_memory client-closure fix squashed into dev cef3f4e5b164633eb143bf6b6a23df3fff339649 (#746, in v5.4.1). app/node/nats_memory.py at the branch tip equals the squash.
- `44b0c0e31cd935d0d53c6f16f5d12a0afcf745b2` DUPLICATE-OF-DEV: upstream branch nodes-shared-storage lease tracking squashed into dev cef3f4e5b164633eb143bf6b6a23df3fff339649 (#746, in v5.4.1). nats_memory.py and its tests at the branch tip equal the squash.
- `4e433f21cbcdceb28e9b2f763fe33ce122644c4f` DUPLICATE-OF-DEV: upstream branch nodes-shared-storage leadership reclaim logic squashed into dev cef3f4e5b164633eb143bf6b6a23df3fff339649 (#746, in v5.4.1). app/nats/leader.py at the branch tip equals the squash.
- `769c9c2b00d58458939cd8110dbf1759869e3716` DUPLICATE-OF-DEV: upstream branch nodes-shared-storage origin-in-sync-messages change squashed into dev cef3f4e5b164633eb143bf6b6a23df3fff339649 (#746, in v5.4.1). manager_sync.py and its tests at the branch tip equal the squash.
- `796521872b5f924be8383ab35d629d07293a5149` DUPLICATE-OF-DEV: upstream branch nodes-shared-storage leader-key logging fix squashed into dev cef3f4e5b164633eb143bf6b6a23df3fff339649 (#746, in v5.4.1). app/nats/leader.py at the branch tip equals the squash.
- `8c6d381b5ba06a01629748d8cd21cb0bdf3869a9` DUPLICATE-OF-DEV: upstream branch nodes-shared-storage CAS logging squashed into dev cef3f4e5b164633eb143bf6b6a23df3fff339649 (#746, in v5.4.1). kv_cas.py and leader.py at the branch tip equal the squash.
- `90dd03a9a0308c2a84ca8d187fcd8bbf9795700a` DUPLICATE-OF-DEV: upstream branch nodes-shared-storage leadership-loss callback squashed into dev cef3f4e5b164633eb143bf6b6a23df3fff339649 (#746, in v5.4.1). app_factory.py and leader.py at the branch tip equal the squash.
- `d26d1eb4c9d2e339ee9434931c93e09c90586410` DUPLICATE-OF-DEV: upstream branch nodes-shared-storage leader _parse exception fix squashed into dev cef3f4e5b164633eb143bf6b6a23df3fff339649 (#746, in v5.4.1). app/nats/leader.py at the branch tip equals the squash.
- `e4ac2db942e8f34b722cda3fa9feb8e53b517b30` DUPLICATE-OF-DEV: upstream branch nodes-shared-storage NATS user sync/lifecycle base commit squashed into dev cef3f4e5b164633eb143bf6b6a23df3fff339649 (#746, in v5.4.1). All 26 branch files equal the squash, apart from three dev-only deltas (5.2.1 version, uvicorn 0.52.1, FinalMask exclude_none).
- `e866dccd4f9f39cb6d619772a719b1764099369e` DUPLICATE-OF-DEV: upstream branch nodes-shared-storage multi-worker NATS refactor squashed into dev cef3f4e5b164633eb143bf6b6a23df3fff339649 (#746, in v5.4.1). app/nats/__init__.py, router.py and app_factory.py at the branch tip equal the squash.
- `eaf89f87e66e1e65ae0d7fd69473aef2a3e16682` DUPLICATE-OF-DEV: upstream branch nodes-shared-storage leader-only node_checker refactor squashed into dev cef3f4e5b164633eb143bf6b6a23df3fff339649 (#746, in v5.4.1). app/jobs/node_checker.py at the branch tip equals the squash.
- `f2c0041395bf6c899b7995b4b459e07a7099bf03` DUPLICATE-OF-DEV: upstream branch nodes-shared-storage CasKv key listing/upsert squashed into dev cef3f4e5b164633eb143bf6b6a23df3fff339649 (#746, in v5.4.1). app/nats/kv_cas.py at the branch tip equals the squash.
- `f6f6236da3339f86a20b770bc8b30b15dbf03898` DUPLICATE-OF-DEV: upstream branch nodes-shared-storage heartbeat status/CAS-exhaustion logging squashed into dev cef3f4e5b164633eb143bf6b6a23df3fff339649 (#746, in v5.4.1). app/node/nats_memory.py at the branch tip equals the squash.
- `327afccb4c36d6bac606b140a428964f5bd17248` DUPLICATE-OF-DEV: upstream branch pref-boost fix #888 was squashed into 8bff2b752aa6b3196a57599126d8239dadb45ce5 (#893), which is in v5.4.1. The fork has it at app/db/crud/user.py:1485-1491 (`_subscription_update_read_session` on a fresh `AsyncSession(bind=db.bind)`).
- `85e5f3e9a44e107f5b27c7daaa3addd209d0af0a` DUPLICATE-OF-DEV: upstream branch pref-boost subscription-update/inbound-tag work squashed into 8bff2b752aa6b3196a57599126d8239dadb45ce5 (#893, in v5.4.1). The branch tip equals the squash for every touched file, and the fork's sub_update_buffer.py is the same blob.
- `ce739e2f406fecb1bc0b14f1e7c26a8579243a09` DUPLICATE-OF-DEV: upstream branch pref-boost fix #886 squashed into 8bff2b752aa6b3196a57599126d8239dadb45ce5 (#893, in v5.4.1). The fork has the stale-update discard at app/subscription/sub_update_buffer.py:99-106 (with_for_update + live_records).
- `03c8b5fc723bc47de55c92fe518783636dc50936` DUPLICATE-OF-DEV: upstream branch pref-boost #892 NATS user-sync optimisation squashed into 8bff2b752aa6b3196a57599126d8239dadb45ce5 (#893, in v5.4.1). The fork's app/nats/kv_index.py, kv_watch.py, kv_cleanup.py, kv_cas.py and app/node/nats_memory.py are the same blobs as the branch tip.
- `bb9c3486285bd7bc19ee75901786669a148a1d5a` DUPLICATE-OF-DEV: upstream branch pref-boost FinalMask fixture fix squashed into 8bff2b752aa6b3196a57599126d8239dadb45ce5 (#893, in v5.4.1). The fork has it at tests/api/test_host.py:350 (`"packet": [1, 2, 255]`).
- `fbd57c75fae02e6c5610bffc4ba3758e7810181f` DUPLICATE-OF-DEV: upstream branch pref-boost compaction timeout squashed into 8bff2b752aa6b3196a57599126d8239dadb45ce5 (#893, in v5.4.1). The fork has app/jobs/cleanup_node_sync.py:23 `asyncio.timeout(240)`, sub_update_buffer.py:33 `_drain_lock` and operation/subscription.py:222 `no-store`.
- `08cd0591e76d16dc7625c44a1ddc58cca89f5cc4` DUPLICATE-OF-DEV: upstream branch ruff-format was squashed into 3fbb26d3fa381f01fc68b921ee7bbea21f877bcf (#721, in v5.4.1). `git diff upstream/ruff-format 3fbb26d3f` is empty for the whole tree.
- `632b2f52b1b5f473f872b5ca9fc676a639ed4ffb` DUPLICATE-OF-DEV: upstream branch ruff-format chmod of cli/main.py and pasarguard-cli.py to 100755 is part of 3fbb26d3fa381f01fc68b921ee7bbea21f877bcf (#721, in v5.4.1). Both files are 100755 in the fork.
- `5a3559298e66e510dd04c42c1e05a0683dc8f212` ALREADY-IN-FORK: upstream branch telegram/tests makes local test runs delete the configured SQLite file instead of CWD test.db. The fork already does this in tests/api/__init__.py:125-135 (`_reset_local_database`, called at :147, fork commit ccaf7133f).
- `9e466167d4f6d0ad24ed0a6cbf6556b5a9e9d969` ALREADY-IN-FORK: upstream branch telegram/tests pathlib rewrite of the same configured-SQLite-path deletion. The fork already covers this in tests/api/__init__.py:125-135 (`_reset_local_database` removes the path plus -wal/-shm).
- `857ead985c03717164f8024ce3fc80570382b48e` upstream/dev reliable node user synchronization (#896), ported by design on release/v5.34.25-p5 (d46a03b65..9b5ca1182): the fork's multicore connect_node, extra cores and attach push are re-expressed inside the full-sync fence with the fresh-session snapshot, per-node snapshot reuse, the fenced add_backend and the recovered-node resync; every touched upstream file is listed in OVERRIDES.md.
- `20858b02009b4fb49ed99c1e9804fa1a889e28c2` upstream/dev bumps pasarguard-node-bridge 0.9.1 to 0.9.2, ported on release/v5.34.25-int: the requirement is >=0.9.2 and [tool.uv.sources] pins Free-Guy-IR/node_bridge_py at rev 6116922a0026a0666a93564265c2d1a375369628 (0.9.2+singbox, the upstream-identical line p5/stage-a-0.9.2), which app/fork/node_bridge_guard.py requires at startup. The earlier pins a9cbcde (9b5ca1182) and deb3410 (d280f915c) carried the parked reject-isolation variant and were never merged.

## Waived

- `517f2bac48cdeb4f570a29b6ea94a3f5cd8158cf` upstream/dev rewrites .github/workflows/github-notifications-bot.yml, upstream's Telegram push and pull-request notifier that posts with upstream's TELEGRAM_BOT_TOKEN and TELEGRAM_TARGETS. The fork deleted that workflow on purpose in 535c1aa01 because it has neither secret nor channel, so the modify/delete conflict is resolved by keeping the deletion.
- `9350d5967e563377a90889deb3c3626cb972eec4` UNMERGED-WIP: upstream branch deprecated-apis test fixups for dd088ef8f's switch from username to id routes. Upstream never merged it, and dev and the fork tests still call the /{username} routes.
- `dd088ef8ff961db65d5851410f2ded8e0b26de00` UNMERGED-WIP: upstream branch deprecated-apis would replace the legacy /api/admin/{username}, /api/user/{username} and node online_stats/{username} routes with /{id}. upstream/dev (app/routers/admin.py:98) and the fork (:98) still serve /{username} alongside /by-username/ and /by-id/ from 71a739d4f (#410). If upstream ships it, that is a breaking API change for username-addressed clients of the fork.
- `f936134cb19783a0871243d4904c088722b9ec26` UNMERGED-WIP: upstream branch deprecated-apis test-only follow-up (tests/api/test_bulk.py) to the unmerged dd088ef8f id-route refactor. Nothing to port while that refactor stays unmerged.
- `7ad4c7d3cee9bb479237eeb34198d61c554b4eb4` UNMERGED-WIP: upstream branch feat/apikey-request-limit adds per-API-key max_requests/request_count/delete_on_limit (migration 1a2b3c4d5e6f) and a counter write on every API-key authentication. No max_requests exists on upstream/dev or in the fork. It is an unmerged feature, not a fix.
- `ab3a72e63f91ef3b9f7232031ccb09ed03094b87` UNMERGED-WIP: upstream branch free-threaded is a May 2026 experiment built on v3.2.1 and 631 commits behind dev. It moves the image to free-threaded CPython 3.14t and drops psycopg/pymysql. upstream/dev and the fork Dockerfile still use `ARG PYTHON_VERSION=3.14`, so there is nothing to port unless upstream adopts 3.14t.
- `e15fc34f5a082e10244f14a55fc6566e25cd5081` UNMERGED-WIP: upstream branch free-threaded follow-up (uv-installed 3.14t in a debian base image, PYTHON_GIL=1 in CI) to the unmerged ab3a72e63 experiment. It has no counterpart on upstream/dev or in the fork.
- `7b08b03960a12894b522565eed78b108402c4e83` UNMERGED-WIP: upstream branch hosts-group adds a hosts_groups_association table (migration 10f64e7f6e2c) and per-inbound host filtering in subscriptions. The table does not exist on upstream/dev or in the fork. It is unmerged feature work that would need an alembic merge and a share.py port if upstream ships it.
- `18a02ed066cba5581eb40a3be12004e3dff4ce4d` UNMERGED-WIP: upstream branch mtp is upstream's own unmerged MTProto implementation (ProxyProtocol.mtproto = 7, app/core/mtproto.py), and upstream/dev has no MTProto. The fork already runs an independent MTProto (app/fork/cores/mtproto.py, app/models/protocol.py:13 mtproto = 9, where 7 is the fork's hysteria2), so if upstream ships this a port must reconcile the enum values and settings.
- `dc988bfb3e1d666563a59b335935905939ac2591` UNMERGED-WIP: upstream branch node-online-users-count adds a per-node online-user count (app/node/online_users.py, NATS topic NODE_ONLINE, NodeResponse.online_users). Neither NODE_ONLINE nor mark_users_online exists on upstream/dev or in the fork. It is an unmerged feature, not a fix.
- `175482516537f2ac14e68067f813dcff4f3356c4` UNMERGED-WIP: upstream branch pre-admin-UserTemplates adds per-admin user templates (user_templates.admin_id, migration 4b348c37504f, templates scope permissions). UserTemplate has no admin_id on upstream/dev or in the fork. It is an unmerged RBAC feature, not a fix.
- `8d2222fd611e9d90d1ca709d6ad8c3ae3c190530` UNMERGED-WIP: upstream branch telegram/tests adds a test-only tests/telegram/ suite that upstream never merged; tests/telegram does not exist on upstream/dev or in the fork. There is no production code change to be exposed to.
- `403e9568bd8ebe9e8d6eaad38184833659ee0dbd` UNMERGED-WIP: upstream branch telegram/tests test-only tweak (RESTART IDENTITY) to a TRUNCATE block that exists only on that branch. upstream/dev and the fork tests/test_record_usages.py still use drop_all/create_all.
- `ca008096ede9b6e189c10a2c8af8750bd6dbf132` UNMERGED-WIP: upstream branch telegram/tests test-only fixes (FakeMessage.answer_photo, TRUNCATE instead of drop_all in tests/test_record_usages.py, and a CWD test.db deletion the branch later replaced). None of it is on upstream/dev or production code.
- `ce485d7e04f14ca3b1dfbb058169d47ab6f93e81` UNMERGED-WIP: upstream branch telegram/tests test-only patch of telegram_settings in tests/telegram/handlers/test_base.py, a file that exists only on that branch.
- `51e57e1471078391693db7b2a8679c0de929134d` UNMERGED-WIP: upstream branch telegram/tests adds 35 lines of test cases to tests/telegram/handlers/admin/test_user.py, a file that exists only on that branch. Test-only, with no production change.
- `1ad2a186348bb5913b2f873bd3684babb8598885` upstream bot branch coderabbit/improve-changed-function-docstrings/1c06464e adds docstrings to the amaziaWG code only. Its pull request PasarGuard/panel#933 into amaziaWG was closed without merge on 2026-09-24; the fork does not carry the AmneziaWG code it documents and keeps no explanatory docstrings.

### Pending feature branches (re-checked by date)

Upstream feature work that is not merged yet. Each entry covers its commit only until its date; after that the gate
fails and the branch has to be looked at again: port it once upstream merges it, extend the date while it is still in
review, or waive it for good if upstream abandons it.

- `96bf290088f26135a3c44c7b323e06958715fc73` pending: tracked feature branch upstream/amaziaWG (AmneziaWG v1-v3.1 for WireGuard hosts and subscriptions, commit "feat(host): Add amneziaWG to host"). Checked 2026-09-25: its pull request PasarGuard/panel#932 into dev is open with comment-only reviews and was still being revised on 2026-09-24, and upstream node dev 4a9d0cf has no AmneziaWG support, so the feature is not merge-ready upstream; port it when #932 merges. until: 2026-10-25
- `0a0eca1e88c4b8881287b3e23753e745e92a2701` pending: tracked feature branch upstream/amaziaWG (AmneziaWG v1-v3.1 for WireGuard hosts and subscriptions, commit "feat: Add amnezia v3.1"). Checked 2026-09-25: its pull request PasarGuard/panel#932 into dev is open with comment-only reviews and was still being revised on 2026-09-24, and upstream node dev 4a9d0cf has no AmneziaWG support, so the feature is not merge-ready upstream; port it when #932 merges. until: 2026-10-25
- `96a6304aa2e1f4f32af66ae6c25fa282f07bfede` pending: tracked feature branch upstream/amaziaWG (AmneziaWG v1-v3.1 for WireGuard hosts and subscriptions, commit "feat(host): Add v3.1 preset configuration for WireGuard Amnezia"). Checked 2026-09-25: its pull request PasarGuard/panel#932 into dev is open with comment-only reviews and was still being revised on 2026-09-24, and upstream node dev 4a9d0cf has no AmneziaWG support, so the feature is not merge-ready upstream; port it when #932 merges. until: 2026-10-25
- `6352825619d67a0158340bee3df3df415d473ec4` pending: tracked feature branch upstream/amaziaWG (AmneziaWG v1-v3.1 for WireGuard hosts and subscriptions, commit "remove: amneziaWG from sing-box"). Checked 2026-09-25: its pull request PasarGuard/panel#932 into dev is open with comment-only reviews and was still being revised on 2026-09-24, and upstream node dev 4a9d0cf has no AmneziaWG support, so the feature is not merge-ready upstream; port it when #932 merges. until: 2026-10-25
- `e04b35fcac70071c0a85019debc188481ed6991e` pending: tracked feature branch upstream/amaziaWG (AmneziaWG v1-v3.1 for WireGuard hosts and subscriptions, commit "Fix CodeRabbit comments on this PR"). Checked 2026-09-25: its pull request PasarGuard/panel#932 into dev is open with comment-only reviews and was still being revised on 2026-09-24, and upstream node dev 4a9d0cf has no AmneziaWG support, so the feature is not merge-ready upstream; port it when #932 merges. until: 2026-10-25
- `cf4bebfaac3d10fde14640badb6903c54f60defd` pending: tracked feature branch upstream/amaziaWG (AmneziaWG v1-v3.1 for WireGuard hosts and subscriptions, commit "fix(dashboard): remove leftover merge conflict marker in host-form.ts"). Checked 2026-09-25: its pull request PasarGuard/panel#932 into dev is open with comment-only reviews and was still being revised on 2026-09-24, and upstream node dev 4a9d0cf has no AmneziaWG support, so the feature is not merge-ready upstream; port it when #932 merges. until: 2026-10-25
- `f7a930967ec3a0204467db9060fac39267dabae9` pending: tracked feature branch upstream/amaziaWG (AmneziaWG v1-v3.1 for WireGuard hosts and subscriptions, commit "fix"). Checked 2026-09-25: its pull request PasarGuard/panel#932 into dev is open with comment-only reviews and was still being revised on 2026-09-24, and upstream node dev 4a9d0cf has no AmneziaWG support, so the feature is not merge-ready upstream; port it when #932 merges. until: 2026-10-25
- `5f7c692a3ecbefd2fc37c102e6ab95442ea7f689` pending: tracked feature branch upstream/amaziaWG (AmneziaWG v1-v3.1 for WireGuard hosts and subscriptions, commit "fix: Camouflage Settings section in the WireGuard host modal tabs"). Checked 2026-09-25: its pull request PasarGuard/panel#932 into dev is open with comment-only reviews and was still being revised on 2026-09-24, and upstream node dev 4a9d0cf has no AmneziaWG support, so the feature is not merge-ready upstream; port it when #932 merges. until: 2026-10-25
- `02dde45a51e5e992cdfecd0798467aaaf264b418` pending: tracked feature branch upstream/amaziaWG (AmneziaWG v1-v3.1 for WireGuard hosts and subscriptions, commit "Fix CodeRabbit comments on this PR"). Checked 2026-09-25: its pull request PasarGuard/panel#932 into dev is open with comment-only reviews and was still being revised on 2026-09-24, and upstream node dev 4a9d0cf has no AmneziaWG support, so the feature is not merge-ready upstream; port it when #932 merges. until: 2026-10-25
- `92cc7dc80b16f5a8d46e7eaa6a9385dcb81279bd` pending: tracked feature branch upstream/amaziaWG (AmneziaWG v1-v3.1 for WireGuard hosts and subscriptions, commit "fix: update Amnezia properties and validation for random_trailers and disable_cookies"). Checked 2026-09-25: its pull request PasarGuard/panel#932 into dev is open with comment-only reviews and was still being revised on 2026-09-24, and upstream node dev 4a9d0cf has no AmneziaWG support, so the feature is not merge-ready upstream; port it when #932 merges. until: 2026-10-25
- `ac167e4244f1c9fcaeab6ffaf37768832f24b469` pending: tracked feature branch upstream/amaziaWG (AmneziaWG v1-v3.1 for WireGuard hosts and subscriptions, commit "docs: document AmneziaWG validation, subscription generation, and host presets"). Checked 2026-09-25: its pull request PasarGuard/panel#932 into dev is open with comment-only reviews and was still being revised on 2026-09-24, and upstream node dev 4a9d0cf has no AmneziaWG support, so the feature is not merge-ready upstream; port it when #932 merges. until: 2026-10-25
- `2f60c83098103ebf36d7b833f893309c883bc8d7` pending: tracked dependabot branch upstream/dependabot/uv/dev/sqlalchemy-asyncio--gte-2.1.1, which raises sqlalchemy[asyncio] to >=2.1.1. Checked 2026-09-29: upstream has not merged it into dev, and SQLAlchemy 2.1 is a new minor line with deprecations removed, so the fork takes it only together with upstream and after a full MySQL and PostgreSQL run. until: 2026-10-29

## Side repositories (not read by the parity gate)

The parity gate reads only PasarGuard/panel. Every other forked repository is tracked commit by
commit by `scripts/upstream_forks.py` against `upstream-forks/forks.toml`, with one ledger per
repository under `upstream-forks/`.

- Free-Guy-IR/subscription-template: the fork's root commit is upstream 8965004 re-rooted, so the
  two histories share no commit (checked 2026-09-24 against upstream main ba2d7865d618, latest tag
  v2.2.1). The panel does not build or ship it: the subscription page in production is the
  standalone Free-Guy-IR/pasarguard-sub-page index.html installed through
  CUSTOM_TEMPLATES_DIRECTORY, and the React src/ in that repository is dead code. Each upstream
  commit has its decision in `upstream-forks/subscription-template.md`. If the fork ever builds the
  page from subscription-template again, it must be rebased onto upstream first.

## Upstream PR candidates (not read by the parity gate)

Upstream bugs the fork fixes in its own code. Main submits them upstream after full verification.

- On-hold timeout action applied to users who came online (upstream d5e6415451a6, #901; in the
  fork as 36d8a382532d): `on_hold_to_active_users_job` runs the disable or delete action on every
  user `get_on_hold_to_active_users` returns, and that query also returns users whose `online_at`
  passed their `edit_at`, i.e. users who connected. With "delete" selected, a user's first
  connection gets it deleted on the next tick; with "disable", disabled. The fork activates the
  users that came online and applies the action only to users whose timeout passed with
  `online_at` still empty (app/fork/jobs/status_review.py, `CAME_ONLINE` / `TIMED_OUT`). Tests:
  tests/test_on_hold_timeout_action_under_lock.py::test_a_user_that_came_online_is_activated_whatever_the_timeout_action,
  ::test_a_first_connection_billed_after_the_read_activates_instead_of_the_timeout_action,
  ::test_disable_action_disables_a_timed_out_user_and_pushes_it and
  ::test_delete_action_removes_a_timed_out_user_from_the_nodes_before_the_row.
- The plain WireGuard .conf writes `Reserved` into [Interface], which wg setconf and the official
  clients reject (see the app/subscription/wireguard.py entry in OVERRIDES.md; test
  tests/test_subscription_wireguard_conf.py).

## Known future conflicts

- upstream/mtp (18a02ed066cba5581eb40a3be12004e3dff4ce4d, waived above as unmerged work) defines `ProxyProtocol.mtproto = 7`. In the fork 7 is hysteria2 and mtproto is 9 (app/models/protocol.py). If upstream releases MTProto, the port must keep the fork's enum values, map upstream's 7 to the fork's 9 wherever it is persisted or sent to nodes, and reconcile upstream's app/core/mtproto.py and settings with the fork's app/fork/cores/mtproto.py.
