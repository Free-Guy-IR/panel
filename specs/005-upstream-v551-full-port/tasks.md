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
  not reopen listeners. Re-decide ledger line 147.
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
  (`alembic downgrade e79833e2a0b2` + previous image), post-deploy dashboard render and api bundle check.

## Dependencies & Execution Order

T001 → T002 → Wave 0 (T010-T015) → Wave 1 (T020, T030, T040, T050, T060, T070 in parallel) → Wave 2 (T080-T082) →
T090-T091 → T100 → T101. Inside each lane, picks follow upstream topological order (`owed-commits.md` column
"owed predecessors").

## Parallel Team Strategy

Wave 1 runs 6 agents (B-AUTH, B-USER, B-NODE, B-SUB, B-DASH, B-INFRA) plus at least one independent verifier and
one hostile-review agent per merged batch.
