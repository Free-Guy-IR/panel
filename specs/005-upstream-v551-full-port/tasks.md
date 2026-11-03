# Tasks: Full port of upstream PasarGuard panel v5.5.1

**Input**: `spec.md`, `plan.md`, `owed-commits.md` (per-commit SHA, PR, files, security, risk, predecessors)

## Format: `[ID] [P?] [Batch] Description`

## Definition of done for EVERY batch task (applies to T010-T090)

Each batch task is done only when all of these hold, and each item is evidence in the batch report:

1. **RED first**: for each commit, the upstream test (or a new one) is committed first and shown failing on
   `0395b8c38` (or on the batch base), then green after the pick.
2. **Interleaving test** for any commit touching state, sync, auth, RBAC, API keys, passkeys, billing, usage or node
   pushes: concurrent writers, readers and node pushes overlapping; asserts no newer update is lost, overwritten or
   replayed stale.
3. **DB matrix**: full suite green on MySQL 9.7, MariaDB 10.6, PostgreSQL 16 and SQLite (never `TEST_FROM=local`
   alone: it silently forces SQLite).
4. **Picks** are `git cherry-pick -x` in upstream topological order; fork-relocated code (`app/fork/`,
   `dashboard/src/fork/`) gets the same change, named in the commit body; no explanatory code comments.
5. **Independent verifier** (not the porter) re-runs every RED test on the final batch head.
6. **Hostile rounds**: fresh agents review the final diff as a hostile upstream maintainer until a full round finds
   nothing new; every finding fixed with a regression test or refuted with evidence.
7. **Ledger**: queue lines of ported commits removed, `Ported by design`/`Waived` lines written with reasons,
   `OVERRIDES.md` updated, gate run shows the batch's commits as ported; codex-review and the 3/3 gate pass; merged
   to `main` the same day; CI green for that exact SHA.

## Phase 1: Setup

- [ ] T001 Land B0 `b0/ledger-20261102` (`51c6ddd67`) on main: main's gate fails today (24 unrecorded dev commits,
  2 refs-snapshot errors). Verify gate exit 0 on the merged head.
- [ ] T002 Land B1 `b1/upstream-units-20261102` (`999aefee6`): 5 picks; gate shows owed 171.
- [ ] T003 [P] Stand up the DB matrix runners (MySQL 9.7, MariaDB 10.6, PG16, SQLite) for all batch branches.
- [ ] T004 [P] Read-only production table sizes (`information_schema.TABLES`) for `users`,
  `user_subscription_updates`, `users_groups_association`, `inbounds_groups_association`, `nodes`, `core_configs`;
  record in the KG.

## Phase 2: Wave 0 - B-DB schema (blocks waves 1 and 2)

- [ ] T010 [B-DB] Add six upstream revisions byte-identical (`b7e2c4d91f60`, `86f7c2a14d3e`, `d73f8a2c4e91`,
  `a4d8c7e91b32`, `a8c2d491e705`, `c8d2e4f6a1b3`) and the fork merge revision with
  `down_revision = ("e79833e2a0b2", "c8d2e4f6a1b3")`; RED test: `alembic heads` single head, schema of fresh install
  equals upgrade path on all four engines.
- [ ] T011 [B-DB] `app/db/models.py`: schema hunks of `61bf853e5e8e`, `b984fa3ceb98`, `819c9b50719c`,
  `4008dcbbe1a6`, `0097556b2181`, plus whole picks of `0cce8f15eb6b` (IdMixin) and `8ece69443478` (cpu_usage Float)
  and `4008dcbbe1a6`; autogenerate diff empty.
- [ ] T012 [B-DB] Idempotence test: `bfd88026565b` then `a8c2d491e705` leaves templates byte-equal; downgrade does
  not reopen listeners. Ledger line 147 (`2d0ca814a798`) was re-decided by main on 2026-11-03: keep both.
- [ ] T013 [B-DB] PG16 order test: `c8d2e4f6a1b3` before and after `f7c63e209a37` (JSONB); MASQUE password vs the
  proxy-secret claim registry (`e79833e2a0b2`) and `6698b9a695e3` sanitize rules.
- [ ] T015 [B-DB] Land the `app/models/proxy.py` MASQUE model hunk of `0097556b2181` with the migration. RED test:
  user exists, upgrade to head, modify the user through the API, `proxy_settings.masque.password` is still there.
  No deploy of a build that has `c8d2e4f6a1b3` without this hunk.
- [ ] T014 [B-DB] Production-size MySQL 9.7 rehearsal: timing, `performance_schema` proof of INPLACE (no COPY) for
  index builds, mysqld peak RSS, `lock_wait_timeout` behaviour with a held FK metadata lock on `nodes`; downgrade to
  `e79833e2a0b2` and re-upgrade. Use a host with the production RAM; on omnidev only under `flock /root/heavy.lock`,
  never prod-named containers.

## Phase 3: Wave 1 - six parallel lanes (disjoint files, verified)

- [ ] T020 [P] [B-AUTH] 16 commits, security first: `eb50bfd58220 41eee936a7f5 14c30eef2c20 15a61fc85b73
  9f437941cf7a 4eff5d9b672a 1481ffab34b5 1a6245d9c63a ce5c68f50e59 f5eb2c1889c5 fd5e95e45a48 180b4ddb1039`, then
  `d1d8f38d2cef 9ee9494cc277 80d9501528bf 65695ac50f6e`. Interleaving tests: API key update vs role change, passkey
  challenge consumption under concurrent logins, token invalidation vs admin replace.
- [ ] T030 [P] [B-USER] 9 commits: `c600c6ab1511 aa592585c4c7 02ac0052b7f7 0c9b1e58e99b ab45a612c6b3 4574d0c5c017
  db3ccde90abe 38ec572d475e 716576e58d86`. Interleaving: bulk quota/expiry vs usage recording; usage writes vs node
  delete; next-plan activation vs reset.
- [ ] T040 [P] [B-NODE] 12 commits: `926f4274f854 a5b28f30ad9c 18dd715f317b 10ad6805f5ca 52e3823b0304 7487b4df89ec
  922d235b012e 4eac02ff9d56 04547de93b8b 1242cc5a7920 ef01e1be578d 663a57edf5fe`. Interleaving: lifecycle preempting
  user sync, heartbeat vs restart, core delete vs node restart; respect the fork's per-user node channel and pending
  removals.
- [ ] T050 [P] [B-SUB] 16 commits: `e538dc14130b` first, then `fe7c5f90397c 9d69fee75145 2aaa5c1f219f 5a636058dc55
  63f991514f5c a7b10f311594 77795ab23b77 183f2925968e 25554abdbf9f ba27c1cca181 3cb009ed68eb b2a2c146ced5
  2834d8d75b51 8269cf282be2 35a2180cb024`. Keep the fork's Hysteria2 sing-box embedding, sub-page escaping and
  WireGuard host overrides; test with a real user's subscription link (probe user).
- [ ] T060 [P] [B-DASH] 39 commits (see `owed-commits.md`, batch B-DASH wave 1), security first: `c9b445a05300
  a1d697b4cd17 f24fdb7d5bd4`. Owns locales, `package.json`, `bun.lock`, `service/api/index.ts` (regenerate with
  orval, keep the mutator contract). XSS guard: i18n `escapeValue: true` stays; test that markup in a translation
  value renders as text. Browser check of the built dashboard (login, users, hosts, core editor, nodes).
- [ ] T070 [P] [B-INFRA] 29 dependency and CI commits (B-DB agent after wave 0): `pyproject.toml`, `uv.lock`, CodeQL
  workflows; `uv lock` regenerated and diffed; security bumps `e2707d99064e` (cryptography), `4f446d8b426c`
  (aiohttp), `25f2dc38fb31` (mako), `80d855f03557` (node bridge 0.9.4, needs bridge parity check) first.

## Phase 4: Wave 2 - serial lane (41 commits, after wave 1 merges)

- [ ] T080 [Wave2] Pick in this order, one at a time, each with its own RED and interleaving test, full DB matrix
  after every 5 picks and at the end: `76b42feedeac 9a743424ee27 0fa51f475322 14d8bbcc0e4a 8420c2c286de
  7f670cf30f62 cc7c7108f44f 61bf853e5e8e 1f694cdea821 3ef603b347cf b984fa3ceb98 819c9b50719c 0aad1b794653
  83fe90e07d33 ba4a0abb6d36 4cfcdd0a66a2 eb30578050a2 43429179a724 0c869b089278 92acbaef044a 79f04fe7034b
  3b7095940c30 0097556b2181 65f2dadc0f61 9480defb699d 796edfd896d8 1bf4af39b5bf da27446f28e8 3eb41d48aca2
  5e51a80bc95f 2d3517b82014 0b77b6ab49d7 f210158e7427 0155c0e50c5f e0bb1127b6c8 659d4cf0373f 59c3b62b65ea
  75a62d889b07 5115de151387`. Schema hunks already landed in wave 0 resolve as empty.
- [ ] T081 [Wave2] MySQL 9.7 test of node and usage stats after `b984fa3ceb98`/`e0bb1127b6c8`/`4cfcdd0a66a2`/
  `819c9b50719c`: fork `get_node_stats` has no HAVING today (upstream dev `bdce1ac6f` fixes one); a conflict
  resolution in `app/db/crud/node.py` must not reintroduce it.
- [ ] T082 [Wave2] Re-decide `5eb8628cfe6d` and `eba70d68eaa5` (version bumps) as `Ported by design` under the fork
  version scheme; never label a version whose code is not present.

## Phase 5: Ledger closure

- [ ] T090 Re-decide the 6 unreleased duplicates (`07792849ed09 60fd57ea7b5c 6ad9a8413586 79959fbeafd0 e1376a5d10b5
  eb73275ca80a`) as `Ported by design` citing their squash; bump `.upstream-sync-point` and `.upstream-refs`;
  gate on main: owed 0, unported 0, exit 0, with checked upstream SHAs recorded.
- [ ] T091 Two-tree diff `v5.5.1` vs main over upstream-tracked files: every remaining difference is in `OVERRIDES.md`.

## Phase 6: Rollout

- [ ] T100 Test panel: deploy, run real traffic plus concurrent scenarios 24 h, browser check of the dashboard,
  owner «وصل شد» from a real subscription link.
- [ ] T101 Production: backup, migrations with panel and workers stopped, `lock_wait_timeout` set, rollback rehearsed
  (`alembic downgrade e79833e2a0b2` + previous image), post-deploy dashboard render and api bundle check. This deploy
  is the first to run `bfd88026565b` on production, so the #879 loopback rewrite of system template id=2 goes in the
  runbook and the user-visible change list as newly delivered, not as already live.

## Phase 7: Upstream v5.6.0 (released 2026-11-03, 45 commits, wave 3)

Source: `v560-commits.md` (per-commit lane, conflicts, ledger text) and the fleet's
`work/fleet-20261102/PORT-PLAN-v560.md` (production checks, owner decisions O-1 to O-7). The definition of done
above applies to every task here. Each lane starts after its own v5.5.1 picks.

- [ ] T125 [PANEL-LEAD] Today, before the 18:24 UTC gate run: remove the 43 held `dev-only after v5.5.1` lines, add
  the 45 Port-queue lines and owner decision `2026-11-03-main-v560-sync` (10 security-yes commits, until
  2026-12-03) and the held line of `cfdce3765ee7` (pyasn1, branch-only) from `v560-commits.md`, run the gate
  with `--write-refs-snapshot` (5 new heads); gate exit 0 on that head (dry run on `a0b48fcb9`: exit 0).
- [ ] T110 [PANEL-DB] After T010-T015: add `b849540d85bb e5a7c3f9b214 f0420da90ff1 1a2b3c4d5e6f` byte-identical
  (blob hashes in `v560-commits.md`), the `app/db/models.py` hunks of `b063e098e a2de6dee8 547fa96d1`
  (NodeOutboundUsage, NodeStat indexes, `Settings.cleanup` with `default_factory=dict`, APIKey `max_requests`,
  `request_count`, `delete_on_limit`, `is_limited`), and one fork merge revision with `down_revision =
  ("2e045b99a67e", "1a2b3c4d5e6f")` refusing a plain downgrade without `ROLLBACK_FLAG`. RED tests: single head and
  parents (split the existing one-head test), byte-identical blobs, fresh install vs production path
  (`d8b2c4f60a17` → head) same schema and `alembic check` clean on all four engines, a database stamped at
  `1a2b3c4d5e6f` gets every fork revision, MySQL index set of `node_stats` after upgrade and
  `fk_node_stats_node_id_nodes` back after downgrade (no error 1553), `app.fork.db_rollback` round trip to
  `2e045b99a67e` and `d8b2c4f60a17`, ORM insert into `settings` under strict mode.
- [ ] T117 [PANEL-DB] Extend `app/fork/migration_guards.py` `REVISION_OBJECTS` with column repair for
  `settings.cleanup` and the three `api_keys` columns, plus `node_outbound_usages` and both `node_stats` indexes;
  MySQL test leaves each partial state of `f0420da90ff1` and `1a2b3c4d5e6f` and re-runs. Production-size MySQL 9.7.2
  rehearsal from a `--single-transaction` dump: time upgrade from `d8b2c4f60a17`, `lock_wait_timeout=60`, SHOW
  INDEX proof, rollback via `app.fork.db_rollback`, simulated cut.
- [ ] T118 [PANEL-DB] Env-copy tests for `b849540d85bb` and `f0420da90ff1`: the 17 production `.env` names with
  placeholder values, empty dotenv, production-shaped settings row → effective settings equal a frozen dict of
  today's effective config; all 11 moved vars set to non-defaults → copied; `.env` read from the process cwd.
- [ ] T111 [B-AUTH] `0b21948e2fae` lands on main with `port/b-auth` (`5d1514c83`); remove its queue line.
- [ ] T112 [B-USER] `1ccd86d172c8` → `c25e582fb9f0`.
- [ ] T113 [B-INFRA] Workflows `d2795ed9d250` (security) → `dddd3b9d7716` → `155ed57dcec1` (security) →
  `77238f82ca77`; then `2781be689700 e582fe7d9573 86bcd0d0a24d ada6fdf857ff f40673225fcc 4c085f24803e
  4c35d98dc9c5` (security, keep spec 004's emSign strip) `0d57c2c44a57 8e92a1212919 b1fd63493699 de75b25f5581
  a93bdf65a261 d6c8c5032f4c 9e8a0a7deb22`, `uv lock` after each; before any T120 pick. Now, ahead of the
  lane: fork-side `uv lock --upgrade-package pyasn1` to 0.6.5 (four OSV advisories against 0.6.4, reached through
  webauthn), with an OSV check test of the lock.
- [ ] T114 [B-SUB] `2f00acd1e95f` (security) → `8a706e967b53` (security; keep the fork `logger` line and upstream's
  name constants) → `4bdf321cce2a` (host-state cache key `state_v6`: cold cache after deploy, in the runbook) →
  `f16fbfa41c30` (security: XDrive s3 object reaches subscriber JSON; test that only xdrive hosts carry it).
- [ ] T115 [B-NODE] `bb87d76a2772` (gzip; tests: no Accept-Encoding gives identity JSON and `/sub/{t}/links` without
  Content-Encoding, gzip decodes to identical bytes, SSE untouched) → `b1cfc5f73429`.
- [ ] T116 [B-DASH] `238e7312bc9f`; after the v5.5.1 finalmask picks `8e2277de8693` → `921bc7997e21`; after fork
  core-kit parity with upstream core-kit 0.3.0 and xray-config-kit 0.4.2 `8ae9e31c1090`; after W2 `cc7c7108f44f`
  `2f760968a46f` → `9e68477be176` → `ae8681539d74`; after T120's `9708b0cd4be7` `bef908793b6b`. Fork XSS regression
  test on every merge; browser render check.
- [ ] T120 [Wave2] After T080-T082 and T111-T116: `d4d2fd423846` → `da158b5729e4` (security: 5000-bucket cap) →
  `f6ebdf0fea4e` (move every fork reader of the removed config attributes to the settings getters; import test of
  each module; keep the fork node-stats guard that excludes only SQLite) → `b063e098e90e` → `bdce1ac6f288` (no
  HAVING reintroduced) → `a2de6dee8283` (fork retention jobs NODE_STATS/NODE_USER_USAGES/NODE_USAGES_RETENTION_DAYS
  stay) → `019e70d695b1` → `547fa96d1051` (T121) → `9708b0cd4be7` (security: S3 secret fields in subscriber JSON;
  needs `8ae9e31c1090` and `f16fbfa41c30`); orval regenerated after each pick that touches `api/index.ts`.
- [ ] T121 [Wave2] `547fa96d1051` API-key request limits: RED and interleaving tests on MySQL and PostgreSQL (about
  50 concurrent Mirza-shaped requests on one unlimited owner-shaped key while the key's note is edited and another
  key revoked: every status unchanged, key intact; limited key K with about 3K concurrent requests: exactly K
  succeed, count K; with `delete_on_limit`: exactly K, one delete, one notification, then 401); BOT-COMPAT clean
  template flow (201, 201, 200, 200, 200, 200, 200, 204, 404). If owner decision O-5 takes the seam: the increment
  runs only when `max_requests` is set (fork predicate module, one-line guard, OVERRIDES.md) with a test that an
  unlimited key issues no `UPDATE api_keys` and survives an injected `OperationalError`.
- [ ] T122 [Wave2] Last: `2b75db15cb00` re-decided `Ported by design` under the fork version scheme only after the
  other 44 are on main.
- [ ] T123 [PANEL-LEAD] Owner decisions O-1 to O-6 of PORT-PLAN-v560.md section F recorded before T100 (outbound
  usage FK policy, node health debounce, `GZIP_ENABLED=false` for the production upgrade, node-stats guard,
  api_keys counter seam, retention off).
- [ ] T124 [PANEL-LEAD] After T122: gate on main owed 0 for v5.6.0; two-tree diff `v5.6.0` vs main, every remaining
  difference in `OVERRIDES.md`; branch-only squash sources re-decided `Ported by design` (list in section E).

## Dependencies & Execution Order

T001 → T002 → Wave 0 (T010-T015) → Wave 1 (T020, T030, T040, T050, T060, T070 in parallel) → Wave 2 (T080-T082) →
T090-T091 → T100 → T101. Inside each lane, picks follow upstream topological order (`owed-commits.md` column
"owed predecessors"). v5.6.0: T125 today; T110 → T117/T118 after wave 0; T111-T116 after each lane's v5.5.1
picks; T120 → T121 → T122 after wave 2; T123 before T100; T124 last. The rollout tasks T100/T101 then carry v5.6.0.

## Parallel Team Strategy

Wave 1 runs 6 agents (B-AUTH, B-USER, B-NODE, B-SUB, B-DASH, B-INFRA) plus at least one independent verifier and
one hostile-review agent per merged batch.
