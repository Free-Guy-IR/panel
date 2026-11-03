# Implementation Plan: Full port of upstream PasarGuard panel v5.5.1

**Branch**: `spec/upstream-v551-full-port` | **Date**: 2026-11-03 | **Spec**: `spec.md`

## Summary

Port the 176 owed upstream commits (B0 count, verified) in four waves with exclusive file ownership, so 6 agents
work in parallel in wave 1 without touching the same file. Wave 0 lands the schema (six upstream revisions plus one
fork merge revision). Wave 2 is one serial lane for the 41 commits that span several batches. Every batch: RED test
first, interleaving test where state is touched, DB matrix, independent verifier, hostile rounds, ledger update.
Upstream v5.6.0 (released 2026-11-03) adds 45 owed commits and four revisions, ported as wave 3 in the same lanes
(section "Upstream v5.6.0", tasks T110-T125).

## Technical Context

Python 3.12, FastAPI, SQLAlchemy 2 async, Alembic; dashboard React + TypeScript (bun, orval).
DB engines under test: MySQL 9.7 (production), MariaDB 10.6, PostgreSQL 16, SQLite.
Gate: `scripts/upstream_inventory.py` with the arguments of `.github/workflows/upstream-inventory.yml`.
Data: `owed-commits.md` / `owed-commits.tsv` (generated 2026-11-03 against main `0395b8c38`).

## Constitution Check

Spec Kit gate before code, codex-review + 3/3 gate before every push, no explanatory code comments, `main` is the
delivery target, nothing pushed with secrets, CI verified after every push. All batches comply by construction of
`tasks.md`.

## Order of landing

0. **B0** (`b0/ledger-20261102`, `51c6ddd67`) lands first: main's gate fails today (24 unrecorded dev commits and 2
   refs-snapshot errors) and B0 fixes that and settles 4 commits. Then **B1** (`999aefee6`): 5 picks
   (`16ce46d8ad6d`, `9866707bc439`, `e886ab6cac3b`, `13d5e6778c3f`, `3ec5635b7599`). Nobody ports those 9 again.
1. **Wave 0 – B-DB schema** (1 agent): migrations and `app/db/models.py`.
2. **Wave 1 – six parallel lanes** with disjoint file sets (checked: zero files shared between lanes, hub files
   included): B-AUTH 16, B-USER 9, B-NODE 12, B-SUB 16, B-DASH 39, B-INFRA 29 (+ B-DB's `4008dcbbe1a6`,
   `0cce8f15eb6b`, `8ece69443478` in wave 0). The B-DB agent runs B-INFRA after wave 0.
3. **Wave 2 – one serial lane** (41 commits, upstream topological order, each on top of the previous):
   `76b42feedeac 9a743424ee27 0fa51f475322 14d8bbcc0e4a 8420c2c286de 7f670cf30f62 cc7c7108f44f 61bf853e5e8e
   1f694cdea821 3ef603b347cf b984fa3ceb98 819c9b50719c 0aad1b794653 83fe90e07d33 ba4a0abb6d36 4cfcdd0a66a2
   eb30578050a2 43429179a724 0c869b089278 92acbaef044a 79f04fe7034b 3b7095940c30 0097556b2181 65f2dadc0f61
   9480defb699d 796edfd896d8 1bf4af39b5bf da27446f28e8 3eb41d48aca2 5e51a80bc95f 2d3517b82014 0b77b6ab49d7
   f210158e7427 0155c0e50c5f e0bb1127b6c8 659d4cf0373f 59c3b62b65ea 75a62d889b07 5115de151387 5eb8628cfe6d
   eba70d68eaa5`. The version bumps `5eb8628cfe6d` (v5.5.0) and `eba70d68eaa5` (v5.5.1) are re-decided as
   `Ported by design` against the fork's own version scheme (the fork never claims a version it does not contain).
4. **Re-decide** the 6 unreleased pre-squash duplicates as `Ported by design` citing their released squash.

Wave-2 can be split into two serial chains only after a file-overlap recheck on the post-wave-1 head; the default is
one chain.

### Hub files (one owner each)

`pyproject.toml`, `uv.lock`, `app/version.py` → B-INFRA. `dashboard/package.json`, `dashboard/bun.lock`,
`dashboard/public/statics/locales/*.json`, `dashboard/src/service/api/index.ts` → B-DASH. A commit outside these
lanes that touches a hub file was moved to wave 2. `api/index.ts` is regenerated with orval after each pick and must
keep the fork mutator contract (`http.ts` signature; see the orval trap of 2026-09); `uv.lock` is re-locked with
`uv lock` and reviewed, never hand-merged.

## Batch B-DB: migration splice (exact plan)

Fork alembic today: one head `e79833e2a0b2` (`proxy_secret_claims`). Fork already carries upstream `9a5c2e7f1b4d`
and `b4c7e8f1a2d3` byte-identical, merged into the fork chain by `fd33c6013baa` (`down_revision = ("e5a9c3f1b7d2",
"b4c7e8f1a2d3")`) and `8158387867c9` (`("0374ac83604b", "bfd88026565b")`).

Upstream v5.5.1 chain after `b4c7e8f1a2d3`:

| revision | down_revision | upstream commit | operation |
|---|---|---|---|
| `b7e2c4d91f60` | `b4c7e8f1a2d3` | `61bf853e5e8e` #774 | index `users(created_at, id)`; PG `CONCURRENTLY` in autocommit block |
| `86f7c2a14d3e` | `b7e2c4d91f60` | `b984fa3ceb98` #865 | index `user_subscription_updates(created_at)` |
| `d73f8a2c4e91` | `86f7c2a14d3e` | `819c9b50719c` #875 | add `sort_order` to `nodes` and `core_configs`, `UPDATE ... SET sort_order = id`, then `NOT NULL` via `batch_alter_table` |
| `a4d8c7e91b32` | `d73f8a2c4e91` | `4008dcbbe1a6` #772 | indexes `inbounds_groups_association(group_id, inbound_id)`, `users_groups_association(groups_id, user_id)`; PG concurrently |
| `a8c2d491e705` | `a4d8c7e91b32` | `2d0ca814a798` #879 | data only: rewrite exposed SOCKS/HTTP listeners in system `xray_subscription` client templates to 127.0.0.1 |
| `c8d2e4f6a1b3` | `a8c2d491e705` | `0097556b2181` #1025 | data only: add `masque.password` to every `users.proxy_settings` JSON |

Splice:

1. Add all six upstream files byte-identical (including `a8c2d491e705`, which the fork already carries in spirit
   as `bfd88026565b`, because `c8d2e4f6a1b3` names `a8c2d491e705` as its parent and editing an upstream file would
   break byte identity for every future sync). Decided by main on 2026-11-03: keep upstream `a8c2d491e705`
   byte-identical beside `bfd88026565b` for chain integrity; both run the same idempotent listener rewrite, so the
   templates end byte-equal whichever runs second (T012 proves it). The ledger entry of `2d0ca814a798` (line 147
   before this decision, which said "keep only bfd88026565b") records it. This creates a second branch from
   `b4c7e8f1a2d3`.
2. Add one fork merge revision `merge_upstream_v5_5_1_migrations_with_fork_head` with
   `down_revision = ("e79833e2a0b2", "c8d2e4f6a1b3")`, no operations, upgrade and downgrade both `pass`.
3. `app/db/models.py`: add `User` index `(created_at, id)`, `UserSubscriptionUpdate` index on `created_at`,
   `sort_order` on `Node` and `CoreConfig`, the two association indexes, `cpu_usage` Float (`8ece69443478`), IdMixin
   dataclass change (`0cce8f15eb6b`), each matching the upstream model so autogenerate shows no diff.
4. Tests: `alembic heads` single head; fresh-DB vs upgrade-path schema equality on all four engines; idempotence of
   `a8c2d491e705` after `bfd88026565b` (templates byte-equal after both); `c8d2e4f6a1b3` on PostgreSQL after the
   fork's JSONB conversion `f7c63e209a37` (fresh install may order the branches differently from the upgrade path,
   so both orders are tested); `alembic downgrade e79833e2a0b2` and re-upgrade.
5. MASQUE passwords must enter the fork's proxy-secret claim registry (`e79833e2a0b2`) if that registry covers all
   per-user proxy credentials; the B-DB agent decides with a RED test, and the sanitize rules of `6698b9a695e3`
   are checked against the new key.
6. Coupling found on 2026-11-03: the fork's `ProxyTable` (`app/models/proxy.py`) has no `masque` field and uses
   pydantic's default `extra="ignore"`, so after `c8d2e4f6a1b3` fills `proxy_settings.masque.password`, any user
   update made by the app before the MASQUE model lands re-serializes `ProxyTable` and drops that password. The
   migration does not run again, so those users would keep an empty MASQUE password. Therefore wave 0 also lands
   the `app/models/proxy.py` MASQUE model hunk of `0097556b2181` (no wave-1 lane touches that file), with a RED
   test: create user, upgrade, modify user, password still present. Until that hunk is on main, no build with
   `c8d2e4f6a1b3` is deployed anywhere.
7. Verified safe: `d73f8a2c4e91` sets `server_default="0"` and the v5.5.1 model has `default=0,
   server_default=text("0")`, so node and core inserts made by the fork's current code before wave 2 succeed.

### Cost on production-size MySQL

Measure first (read-only): `SELECT table_name, table_rows, data_length, index_length FROM information_schema.TABLES
WHERE table_schema = DATABASE()` on production, then rehearse on a restored copy on a host with the same RAM.

- `b7e2c4d91f60`, `86f7c2a14d3e`, `a4d8c7e91b32`: `CREATE INDEX` on InnoDB is online (`ALGORITHM=INPLACE, LOCK=NONE`
  is MySQL's default for a secondary index), no table rebuild. Cost is one scan plus sort per index;
  `user_subscription_updates` can be the largest of the three, so its sort buffer and temp space
  (`innodb_tmpdir`, disk headroom) are checked before the run. The migration files do not pass `mysql_algorithm`;
  the rehearsal confirms with `performance_schema` that no COPY happens, and a fork wrapper is not added unless it does.
- `d73f8a2c4e91`: `ADD COLUMN` nullable is instant; `ALTER ... MODIFY sort_order INT NOT NULL` rebuilds `nodes` and
  `core_configs` in place. Both tables are tiny (tens of rows). The risk is the metadata lock: usage writers hold
  foreign-key metadata locks on `nodes`, so the ALTER waits and queues every later query. Run migrations with the
  panel, node worker and scheduler worker stopped (the normal deploy order) and with `lock_wait_timeout` set low, so
  a blocked ALTER fails fast instead of stalling the panel.
- `a8c2d491e705`: rewrites a handful of template rows; no DDL; idempotent after `bfd88026565b`.
- `c8d2e4f6a1b3`: batches of 1000 users (`id > last_id AND password missing`, ordered by id), one executemany
  UPDATE per batch. On MySQL the filter is `JSON_EXTRACT` on an unindexed JSON column, so each batch scans the rest
  of `users`; at the production user count that is seconds, but it takes row locks on hot `users` rows, so it runs
  with workers stopped. On PostgreSQL the bound value is typed `JSON` while the fork column is `JSONB` after
  `f7c63e209a37`; the assignment cast must be proven by the PG16 test, not assumed.
- No migration changes a foreign key, so the 2026-09 failure mode (FK `ON DELETE` change rebuilding a child table and
  OOM-killing mysqld) does not recur; the rehearsal still records mysqld peak RSS.

### Downgrade safety

Index drops are online. `d73f8a2c4e91` downgrade drops `sort_order` (ordering lost, recoverable by re-upgrade, which
sets `sort_order = id`). `a8c2d491e705` downgrade does not reopen listeners (upstream design; fork `bfd88026565b`
the same). `c8d2e4f6a1b3` downgrade deletes the MASQUE passwords; a re-upgrade generates new ones, which changes
MASQUE links for users; acceptable only while no host uses MASQUE, and the runbook says so. The merge revision
downgrade is `alembic downgrade e79833e2a0b2`, which un-applies the six upstream revisions; a bare `downgrade -1`
from the merge point is ambiguous and is not used.

### Does `a8c2d491e705` change live inbound configs on nodes?

No. It only rewrites rows of the system `xray_subscription` client templates (the JSON the panel sends to client
apps), changing their local SOCKS/HTTP `listen` from an exposed address to `127.0.0.1`. It does not touch
`core_configs`, hosts or anything pushed to nodes, so no node restart or resync follows. The fork's own
`bfd88026565b` (2026-09-25) runs the same rewrite, but production has not run it: on 2026-11-03 production was at
`d8b2c4f60a17` on a build without `2e7fc450f`, and its system template id=2 still served SOCKS and HTTP on `0.0.0.0`
(read-only check, verdicts/panel-spec005.md). The first of `bfd88026565b` / `a8c2d491e705` to run on production
therefore rewrites template id=2 to `127.0.0.1`, a real data change that delivers the #879 security fix; the second
is a no-op. Clients get the loopback template on their first subscription fetch after that deploy.

Checked on 2026-11-03: both files use the same `EXPOSED_LISTENER` and `INBOUNDS_ARRAY` regexes, select only
`template_type == "xray_subscription"` and `is_system`, skip rows whose content does not change, and have an empty
downgrade. Before the deploy the B-DB agent lists, read-only, every system `xray_subscription` template on production
whose listeners the rewrite changes; on 2026-11-03 that was template id=2, both listeners on `0.0.0.0`.

## Security commits (38, `security: yes` in the ledger)

B-AUTH: `eb50bfd58220 41eee936a7f5 14c30eef2c20 15a61fc85b73 9f437941cf7a 4eff5d9b672a 1481ffab34b5 1a6245d9c63a
ce5c68f50e59 f5eb2c1889c5 fd5e95e45a48 180b4ddb1039`; B-DASH: `c9b445a05300 a1d697b4cd17 f24fdb7d5bd4`;
B-INFRA: `e2707d99064e 4f446d8b426c 25f2dc38fb31 80d855f03557`; B-SUB: `e538dc14130b`; wave 2:
`76b42feedeac 9a743424ee27 0fa51f475322 14d8bbcc0e4a ba4a0abb6d36 eb30578050a2 92acbaef044a 3b7095940c30
da27446f28e8 5e51a80bc95f 2d3517b82014 0b77b6ab49d7 f210158e7427 0155c0e50c5f e0bb1127b6c8 59c3b62b65ea`;
plus `0c9b1e58e99b ab45a612c6b3` (B-USER). Security ports are scheduled first inside each lane.

XSS guard: upstream v5.5.1 sets i18n `escapeValue: false` and adds `dashboard/src/utils/html-translation.ts`. The
fork keeps `escapeValue: true` + `shouldUnescape` on `<Trans>` (`9c3ae145f`). Checked on 2026-11-03: the helper came
with upstream `1c2503632`, already `Ported by design` in the ledger (line 150), and none of the 176 owed commits
touches an i18n file or adds `dangerouslySetInnerHTML`, `html-translation` or `escapeValue`. The risk is therefore in
conflict resolution only: B-DASH and wave 2 run the fork's XSS regression test on every merge, plus a test that a
translation value containing `<img src=x onerror=...>` renders as text.

## Upstream v5.6.0 (released 2026-11-03): 45 commits, now owed (wave 3)

Upstream released v5.6.0 on 2026-11-03 (`2b75db15cb00`, 14:18:07Z): the 43 dev commits held below plus
`ae8681539d74` (users table Shamsi dates and ellipsis) and `2b75db15cb00` (version bump). Under the release-only
rule every one is now owed. Upstream dev is `ae8681539d74` (nothing past the release). Full classification,
production checks and ledger text: `v560-commits.md` here and the fleet's `work/fleet-20261102/PORT-PLAN-v560.md`
(PANEL-AUDIT-C round 9).

- **Gate**: `scripts/upstream_inventory.py:1799-1806` fails every held line a final release contains, so the ledger
  re-decision (T125) lands on 2026-11-03, before the 18:24 UTC scheduled run.
- **Lanes** (by file ownership, totals): B-INFRA 18, B-DASH 8, B-SUB 4, B-USER 2, B-NODE 2, B-AUTH 1 (`0b21948e2`
  taken early as `5d1514c83` on `port/b-auth`), W2 10. PANEL-DB takes the schema hunks of `f6ebdf0fe`,
  `b063e098e`, `a2de6dee8`, `547fa96d1`. Each lane starts after its own v5.5.1 picks.
- **Security yes (10)**: `d2795ed9d 4c35d98dc da158b572 f16fbfa41 2f00acd1e 155ed57dc 0b21948e2 8a706e967 547fa96d1
  9708b0cd4`, owed under owner decision `2026-11-03-main-v560-sync` until 2026-12-03.
- **Migration splice (option A)**: the four upstream files byte-identical with `b849540d85bb.down_revision =
  "c8d2e4f6a1b3"`, plus one fork merge revision with `down_revision = ("2e045b99a67e", "1a2b3c4d5e6f")` and the
  `ROLLBACK_FLAG` refusal of `2e045b99a67e`. Rewriting the upstream parent is rejected: it breaks the content proof
  and a database stamped by official v5.6.0 would skip every fork revision. Production cost is negligible
  (node_stats 0 rows, api_keys 7, settings 1; node_user_usages untouched); the risk is an MDL wait under
  `lock_wait_timeout` = 31536000, so migrations run with the panel stopped and the processlist checked.
- **Env vars to settings**: production sets none of the 11 moved vars and fork defaults equal the v5.6.0 setting
  defaults, so `b849540d85bb` and `f0420da90ff1` are no-ops there. Two effective changes need decisions: node health
  debounce (from v5.5.1 #950, already in W2) and gzip on by default (`bb87d76a2`; `GZIP_ENABLED=false` on
  production keeps the upgrade identical). Every fork reader of the removed config attributes moves to the settings
  getters in the `f6ebdf0fe` pick.
- **Bots**: the 7 owner keys keep `max_requests` NULL and are neither limited nor deleted, but `547fa96d1` writes
  `api_keys` on every API-key request; the optional seam (increment only when `max_requests` is set) is owner
  decision O-5 in PORT-PLAN-v560.md, proven by the interleaving test of T121.

Unreleased migrations at the time of the v5.5.1 plan (now released in v5.6.0): `1a2b3c4d5e6f` api_key request limit
(`547fa96d1`), `b849540d85bb` runtime env vars to settings (`f6ebdf0fe`), `e5a7c3f9b214` node outbound usages
(`b063e098e`), `f0420da90ff1` cleanup retention settings (`a2de6dee8`).

Flagged for an early owner decision while v5.6.0 was unreleased (kept for the record):
- `0b21948e2` #1093 fix(admins): prevent limited admins from changing their own limits: privilege escalation fix,
  security yes. Recommend an owner decision to take it early, with its test `tests/api/test_admin_self_limits.py`.
- `bdce1ac6f` #1132 fix(node): `get_node_stats` HAVING clause rejected by MySQL 9 and MariaDB. Checked: the HAVING
  is upstream code from before v5.4.1 (`56cb06e62`), still in v5.5.1, and fork main's `get_node_stats`
  (`app/db/crud/node.py:344`) has none (the last fork change to that string is `1519b6bbb`, 2026-09-22). No
  production risk today and no early port needed; wave-2 conflict resolution in `app/db/crud/node.py` must not
  bring the HAVING back, and the dev fix is later recorded as `Ported by design`.
- `c25e582fb` #1133 remove a deleted user's rows on SQLite (data hygiene, SQLite only).
- `155ed57dc` #1102 CI token permissions per job (supply-chain hardening for upstream CI; fork CI is separate).
- `4c35d98dc` certifi 2026.10.23 (fork already carries the certifi fix per spec 004).

Full list (oldest first): `8e2277de8` TCP FinalMask filter in inbound editor; `921bc7997` UDP noise exp help
popover; `77238f82c` codeql-action 4.39.1; `2781be689` pydantic-settings 2.16.1; `e582fe7d9` asyncmy 0.2.17;
`86bcd0d0a` alembic 1.20.1; `ada6fdf85` ruff 0.17.2; `d2795ed9d` docker/login-action 4.7.0; `dddd3b9d7` CI release
contributors; `f40673225` python-dateutil 3.0.0; `d4d2fd423` REALITY fingerprint compatibility help (#1111);
`4c085f248` sqlalchemy requirement; `4c35d98dc` certifi; `0d57c2c44` fastapi 0.162.0; `1ccd86d17` online users one
pass on PostgreSQL (#1114); `8e92a1212` sse-starlette 3.5.1; `b1fd63493` pytest-asyncio 1.4.1; `de75b25f5`
sse-starlette 3.6.0; `bb87d76a2` gzip responses (#1121); `a93bdf65a` ruff 0.17.3; `da158b572` limit chart periods
(#1128); `f6ebdf0fe` runtime env vars to settings (#1129); `b063e098e` usage history per outbound (#1127);
`bdce1ac6f` node stats on MySQL/MariaDB (#1132); `c25e582fb` deleted user rows on SQLite (#1133); `a2de6dee8`
automatic retention policies (#801); `2f760968a` remove HWID count from users table; `019e70d69` roll old usage rows
into coarser buckets (#1134); `b1cfc5f73` filter core user memberships before aggregation (#1090); `4bdf321cc`
shared inbound encryption stable across restarts (#1087); `f16fbfa41` XDrive s3 settings to clients (#1126);
`2f00acd1e` WireGuard archive entry names flat and unique (#1103); `155ed57dc` CI token permissions (#1102);
`0b21948e2` limited admins cannot change own limits (#1093); `8a706e967` WireGuard archive reserved/Unicode/long
names (#1135); `547fa96d1` API key request limit and auto-delete (#1136); `8ae9e31c1` core-kit 0.3.0 /
xray-config-kit 0.4.2; `9708b0cd4` Xray tun/xhttp/xdrive editor settings; `bef908793` host modal status icon;
`d6c8c5032` pydantic-settings 2.16.2; `9e8a0a7de` pytest 9.2.0; `9e68477be` customizable users table views (#1139).

## Project Structure

```text
specs/005-upstream-v551-full-port/
├── spec.md
├── plan.md
├── tasks.md
├── owed-commits.md
├── owed-commits.tsv
└── v560-commits.md
```

## Complexity Tracking

| Item | Why | Simpler alternative rejected because |
|---|---|---|
| Serial wave 2 (41 commits) | they span lanes; parallel picks would collide on the same files | splitting a commit across agents breaks `cherry-pick -x` content proof |
| Schema-first wave 0 | five feature commits need the columns/indexes; models.py has one owner | letting each lane edit models.py collides |
| Byte-identical `a8c2d491e705` beside `bfd88026565b` | keeps upstream chain intact for every later sync | rewriting `c8d2e4f6a1b3`'s parent edits an upstream file |
