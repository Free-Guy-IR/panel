# Feature Specification: Full port of upstream PasarGuard panel v5.5.1

**Feature Branch**: `spec/upstream-v551-full-port`
**Created**: 2026-11-03
**Status**: Draft (research and planning; no application code changed)
**Input**: Owner order of 2026-11-03: upstream PasarGuard changed tables and more; review everything and port it
into the fork completely and correctly, never half-ported or buggy; then the test panel, then production. Release-only
rule of 2026-10-25: port only upstream final releases; dev-only commits are recorded as held, never ported.

Numbering note: the request named `specs/003-...`, but `specs/003-close-open-audit-items` and
`specs/004-certifi-emsign-distrust` already exist on fork main, so this feature is `005`.

## Measured state (2026-11-03, re-derived with `scripts/upstream_inventory.py`, not by hand)

| Ref | Head | Owed ports | Unported | Gate |
|---|---|---|---|---|
| fork `main` | `0395b8c385c0` | 180 | 24 | **FAILS** (exit 1): 24 upstream dev commits of 2026-10-30..11-02 unrecorded, 2 refs-snapshot errors (dev `9e68477be176`, feat/mtproto `3efd08d6fe8e`) |
| B0 `b0/ledger-20261102` | `51c6ddd67` | 176 | 0 | OK, not at parity; fails on 2026-11-06 |
| B1 `b1/upstream-units-20261102` | `999aefee6` | 171 | 0 | OK, not at parity; fails on 2026-11-06 |

The 176 figure is confirmed exactly for B0. Main shows 180 because B0 settles four more: `cf318d70be6d`, `9df2c64f7b7c`,
`0cad06d4ecdb` (scripts/ helper move, waived) and `e5db1fbfa3ac` (picked as `34bbeccf7`). B1 picks five more:
`16ce46d8ad6d`, `9866707bc439`, `e886ab6cac3b`, `13d5e6778c3f`, `3ec5635b7599`.

Upstream: v5.5.1 `eba70d68eaa5` (2026-10-25), dev `9e68477be176` (42 commits past v5.5.1), base v5.4.1 `b56ffe369f54`.

Of the 176 owed commits: 157 are in v5.5.0, 13 more in v5.5.1, and 6 are in no release because they are the
pre-squash branch copies of released squash commits (`07792849ed09`=`2aaa5c1f219f` #993, `60fd57ea7b5c`=`dba231e8d439`
#986, `6ad9a8413586`=`80d9501528bf` #984, `79959fbeafd0`=`3ef603b347cf` #994, `e1376a5d10b5`=`51e5385a7d7c` #985,
`eb73275ca80a`=`ba27c1cca181` #1010). Under the release-only rule those six are not ported themselves; they are
re-decided as `Ported by design` citing their released squash once that squash is ported.

38 owed commits are `security: yes` in the ledger. `git apply --check` of each owed commit against fork main:
139 CONFLICT, 37 CLEAN. 144 touch at least one file whose fork blob matches no upstream version (fork-diverged).
Risk: 124 HIGH (conflict on fork-diverged code), 35 MED, 17 LOW.

The full per-commit table (sha, PR, date, release, title, batch, wave, security, apply result, risk, fork-diverged
file count, owed predecessors on the same files, every touched file) is `owed-commits.md`, machine-readable as
`owed-commits.tsv`, both in this directory.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Fork at verified parity with upstream v5.5.1 (Priority: P1)

The owner runs the upstream parity gate on fork main and it reports zero owed ports for every commit released in
v5.5.0 and v5.5.1, with every commit either picked with `cherry-pick -x` (content present) or decided with a reason.

**Independent Test**: `scripts/upstream_inventory.py --fail-on-unported UPSTREAM_SYNC.md` on fork main reports
`owed: 0` for released commits and exits 0; the two-tree diff `git diff v5.5.1 main -- <upstream-tracked files>`
shows only lines listed in `OVERRIDES.md`.

**Acceptance Scenarios**:

1. **Given** fork main after all batches merge, **When** the gate runs, **Then** owed is 0 and unported is 0.
2. **Given** any of the 38 security commits, **When** its RED test from upstream is run on the final head, **Then**
   it passes and the same test fails on `0395b8c38`.

### User Story 2 - Schema identical in intent to upstream, safe on production MySQL (Priority: P1)

The six upstream v5.5.x revisions are present, the fork's alembic graph has exactly one head, and upgrading a
production-size MySQL 9.7 copy from `e79833e2a0b2` finishes without a table rebuild of any large table and
without OOM.

**Independent Test**: on a restored production-size dump, `alembic upgrade head` then `alembic downgrade e79833e2a0b2`
then `alembic upgrade head`, with timing and peak RSS of mysqld recorded; repeated on MariaDB 10.6, PostgreSQL 16
and SQLite.

**Acceptance Scenarios**:

1. **Given** a DB at `e79833e2a0b2`, **When** upgraded, **Then** the six upstream revisions and the fork merge
   revision are applied and `alembic heads` lists one head.
2. **Given** a fresh empty DB, **When** upgraded to head, **Then** the schema equals the upgrade-path schema
   (compare `information_schema` / `pg_catalog` dumps).

### User Story 3 - No fork security fix reopened (Priority: P1)

Every fork-only hardening survives the port, in particular the stored-XSS fix `9c3ae145f` (i18n `escapeValue: true`
plus `shouldUnescape` on `<Trans>`), the sub-page explicit escaping, the passkey and RBAC fixes, and the content
filter and traffic-log features.

**Independent Test**: the fork's existing XSS, RBAC and passkey regression tests pass on the final head; a new test
asserts that `dashboard/src/utils/html-translation.ts` from upstream is either not imported or only renders
allow-listed tags, and that i18n init keeps `escapeValue: true`.

### User Story 4 - Rolled out test panel first, then production (Priority: P2)

The ported build runs on the test panel with real traffic and concurrent scenarios, the owner confirms from the
user's subscription link, and only then production is upgraded with a rehearsed rollback.

### Edge Cases

- A commit spans several batches: it is ported in wave 2 by one owner, in upstream order, never split across agents.
- An upstream commit edits code the fork moved under `app/fork/` or `dashboard/src/fork/` (for example the
  WireGuard host layout in `dashboard/src/fork/hosts/`): the port applies the change to the fork location and the
  pick records the mapping in the commit body.
- A dev commit after v5.5.1 fixes a bug present in the released code (for example dev `bdce1ac6f` fixes the
  `get_node_stats` HAVING clause that MySQL 9 rejects; the fork already removed that HAVING, so conflict resolution
  must not bring it back): the batch tests on MySQL 9.7, and any bug that does reproduce goes to the owner for an
  early-port decision.
- A schema-first migration lands before the code that writes the new data (MASQUE passwords): the model hunk lands
  with the migration, or the app drops the data on the next write.
- Upstream reverts its own release behaviour later on dev (for example dev `2f760968a` removes the HWID count column
  that owed `cc7c7108f44f` adds): the release state is ported; the dev change stays held.
- Hub files touched by many commits (`uv.lock`, `pyproject.toml`, `dashboard/public/statics/locales/*.json`,
  `dashboard/src/service/api/index.ts`, `dashboard/package.json`, `dashboard/bun.lock`, `app/version.py`) have one owner each.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Every one of the 176 owed commits ends as one of: picked with `git cherry-pick -x` (all added lines
  present, all removed lines gone at the final head), `Ported by design` with the fork commit cited, or `Waived` with
  a reason and, for `security: yes`, an owner decision. No commit is left in the port queue after the batch ends.
- **FR-002**: Only commits reachable from a final release tag are ported; the 42 dev commits past v5.5.1 stay
  `held until an upstream release` in the ledger (listed in `plan.md`).
- **FR-003**: Each batch writes a RED test first, from the upstream test where one exists, that fails on
  `0395b8c38` and passes after the port.
- **FR-004**: Every change touching state, sync, auth, RBAC, API keys, passkeys, billing, usage or node pushes also
  ships an interleaving test that runs the overlapping writers, readers and node pushes and proves no newer update is
  lost, overwritten or replayed stale.
- **FR-005**: Every batch runs the full test suite on MySQL 9.7, MariaDB 10.6, PostgreSQL 16 and SQLite.
- **FR-006**: The fork alembic graph keeps exactly one head; upstream migration files are kept byte-identical; the
  fork adds one merge revision.
- **FR-007**: No migration rebuilds a large table on MySQL; every index build is online (InnoDB `ALGORITHM=INPLACE,
  LOCK=NONE`, PostgreSQL `CONCURRENTLY`) and every migration has a tested downgrade.
- **FR-008**: The fork i18n keeps `escapeValue: true`; any upstream use of `html-translation.ts` is ported only
  through an allow-list renderer that cannot emit attacker markup.
- **FR-009**: Each batch is verified by an independent verifier that re-runs the RED tests on the final head and by
  hostile review rounds of fresh agents until a full round finds nothing new.
- **FR-010**: The ledger (`UPSTREAM_SYNC.md`, `OVERRIDES.md`, `.upstream-refs`, `.upstream-sync-point`) is updated
  in the same commit series, and the gate passes on every merge to main.

### Key Entities

- **Owed commit**: an upstream commit released in v5.5.0/v5.5.1 and listed in the port queue.
- **Batch**: a named set of owed commits with an exclusive file set (`B-DB`, `B-AUTH`, `B-USER`, `B-NODE`, `B-SUB`,
  `B-DASH`, `B-INFRA`) or the serial wave 2.
- **Ledger**: `UPSTREAM_SYNC.md` plus the inventory gate.

## Success Criteria *(mandatory)*

- **SC-001**: Gate on fork main: owed 0, unported 0, exit 0, with the exact upstream SHAs checked recorded.
- **SC-002**: 38 of 38 security commits ported or decided with an owner decision, each with a passing RED test.
- **SC-003**: Production-size MySQL upgrade of the six revisions completes, with no table rebuild beyond `nodes` and
  `core_configs`, and mysqld peak RSS recorded below the host limit.
- **SC-004**: Test panel runs the build for at least 24 hours of real traffic with zero new errors in logs and the
  owner's «وصل شد» from a real user subscription; then production.

## Assumptions

- B0 lands before any port batch (it makes the gate on main green again and settles four commits).
- B1 lands before wave 1 so its five picks are not ported twice.
- Production DB is MySQL 9.7; test panel and CI cover the other engines.
