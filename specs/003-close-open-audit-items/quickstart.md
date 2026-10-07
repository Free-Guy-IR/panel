# Quickstart: validating v5.34.25

Run everything from the release worktree root. Use the panel virtualenv (`uv run` or `.venv/bin/python -m pytest`).

## 1. Backend tests, both modes
```bash
TEST_FROM=local uv run pytest tests/ -q
TEST_FROM=github SQLALCHEMY_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/testdb uv run pytest tests/ -q
```
- A local PostgreSQL is needed for the second run, for example `docker run -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=testdb -p 5432:5432 postgres`.
- Run `make run-migration` and `make check-migrations` against that database first.
- The new tests listed in tasks.md must all pass.

## 2. Fail-before proof
For each new test, run it on the base commit, or against a scratch revert of the fix, and record the failure in the task hand-off:
```bash
git stash push -u -m v53425-proof -- <fix files>
```
Do not use a bare stash. For B1 and D4, use a scratch copy with the v5.34.22 line restored.

## 3. Dashboard
```bash
cd dashboard && bun install --frozen-lockfile && bun run test && bun node_modules/typescript/bin/tsc --noEmit -p tsconfig.app.json | grep -c "error TS"
```
- The error count must be ≤ 578, the recorded baseline.
- `bun run build` must succeed.

## 4. Ledger gate
```bash
uv run python scripts/upstream_inventory.py --base v5.4.1 --head HEAD --fail-on-override-outside-ledger OVERRIDES.md
```
It must exit 0.

## 5. Lint and no-comment check
```bash
uv run ruff check . && uv run ruff format --check .
```
Also, `git diff 72ad0e4a5 -- '*.py' '*.ts' '*.tsx'` must show no added comment or docstring lines.

## 6. Test server (main session)
1. Deploy the v5.34.25 image.
2. Rehearse the named_locks migration on a copy of production data.
3. Prove each item:
   - A user over the limit is limited within one tick.
   - Editing only a node's name or coefficient keeps connections up, with no Stop in the node log.
   - The chart shows the Deleted nodes series.
   - An MTProto-only user with no secret gets 409.
   - Concurrent same-value creates: no value is stored for two users.

## 7. Production deploy (main session only)
Apply D-G2 from plan.md on the production panel host, then verify `docker inspect` LogConfig and that the file log survives a recreate.

Production runs the v5.34.24-usage-integrity image, tree 72ad0e4a5, with the database at `d8b2c4f60a17` (checked read-only on 2026-10-07). Upgrading it to v5.34.25 applies 15 revisions: the 14 that the advanced rollback in section 8 lists, plus `e5a9c3f1b7d2` (named_locks).

The rollback image is the image production already runs, v5.34.24-usage-integrity, and the official rollback target is its head, `d8b2c4f60a17` (decision of 2026-10-07). No rollback image has to be built before v5.34.25 goes live. Record the v5.34.24-usage-integrity image digest next to the v5.34.25 tag, so that a rollback pins exactly that image.

## 8. Rollback to the v5.34.24 line (main session only)
The official rollback returns the database to exactly `d8b2c4f60a17` and runs the v5.34.24-usage-integrity image again. Do it through one entry point that ships in the v5.34.25 image, with the v5.34.25 image still selected, because only that image carries the downgrades.

Before you start, read what this costs subscription links. v5.34.25 mints `v4,<id>,<ts>,<epoch>` tokens, and the v5.34.24-usage-integrity image reads only `v2` and `v3` tokens (app/utils/jwt.py at 72ad0e4a5). So after this rollback:
- every subscription link v5.34.25 handed out answers 404;
- a link handed out before the upgrade (`v3`) keeps working, unless the user was revoked since;
- a user who holds only a v5.34.25 link needs a fresh one, which v5.34.24 mints as `v3`.

If keeping v5.34.25's links alive matters more than speed, use the advanced rollback at the end of this section instead.

1. Back up the database (mysqldump, pg_dump, or a copy of the SQLite file) and note the running v5.34.25 image tag.
2. Stop the panel and every other container that writes to the database: `docker compose stop pasarguard`.
3. Preview: `docker compose run --rm --no-deps --entrypoint python pasarguard -m app.fork.db_rollback --to d8b2c4f60a17 --dry-run`
   It must print `revisions to remove: 009f2a24cc7b, 0374ac83604b, 6698b9a695e3, 6d8eb8fba21b, 8158387867c9, 9a5c2e7f1b4d, b176d23e434c, b4c7e8f1a2d3, bfd88026565b, e5a9c3f1b7d2, e79833e2a0b2, e9a7edd9de06, f7c63e209a37, fd192e9628a5, fd33c6013baa`.
   The entry point reads the database URL from the image's environment. A URL passed with `--url` must use the async driver the panel itself uses (`sqlite+aiosqlite://`, `mysql+asyncmy://` or `postgresql+asyncpg://`); a sync URL such as `sqlite:///` fails before anything is changed.
4. Roll back: `docker compose run --rm --no-deps --entrypoint python pasarguard -m app.fork.db_rollback --to d8b2c4f60a17`
   Expected steps, in this order: `alembic downgrade d8b2c4f60a17`, `alembic downgrade b4c7e8f1a2d3@-1`, `alembic downgrade 9a5c2e7f1b4d@-1`, then `done: the database is at d8b2c4f60a17`. Exit code 0 is success. Exit code 2 prints `rollback refused: ...`, and any other failure exits non-zero with a traceback. On either, the database stays at the last step printed. Rerun step 4 once the cause is fixed, or restore the backup from step 1.
5. Check: `docker compose run --rm --no-deps --entrypoint python pasarguard -m alembic current` must print exactly one revision line, `d8b2c4f60a17`.
6. Pin the v5.34.24-usage-integrity image by its recorded digest in docker-compose.override.yml and `docker compose up -d`. Its start.sh runs `alembic upgrade head`, which is a no-op at `d8b2c4f60a17`, its own head.
7. Verify in a real browser that the dashboard renders. Check that a subscription link handed out before the upgrade still opens (`/sub/<token>/info` gives 200), and expect a link v5.34.25 handed out to give 404, as described above.

Never run these commands:
- `alembic downgrade d8b2c4f60a17` (or `e5a9c3f1b7d2`) on its own. It removes only that revision's descendants. The upstream passkey branch (`9a5c2e7f1b4d`, `b4c7e8f1a2d3`) forks off `48a6bcb8bba1` and would stay in alembic_version, and the v5.34.24 image then crash-loops on `Can't locate revision identified by 'b4c7e8f1a2d3'`.
  - The fork's merge revision `fd33c6013baa` now refuses that downgrade unless the entry point runs it. The plain command exits 1 with `Refusing to downgrade fd33c6013baa on its own` and leaves the database at `fd33c6013baa`. The main chain above it is already gone at that point, so the v5.34.24 image cannot start on it either.
  - Run step 4 from there. The entry point also repairs the two-head state `b4c7e8f1a2d3, e5a9c3f1b7d2`, which an earlier v5.34.25 build without the refusal could leave.
  - Every other path that would remove `fd33c6013baa` is refused the same way, and step 4 repairs each of them:
    - `alembic downgrade 48a6bcb8bba1` and `alembic downgrade base` stop at `fd33c6013baa`;
    - `alembic downgrade 9a5c2e7f1b4d` stops at two heads, `0374ac83604b, fd33c6013baa`. The fork chain above the target is not a descendant of `9a5c2e7f1b4d`, so that downgrade leaves it in place.
- `alembic downgrade b4c7e8f1a2d3-2`. Without `@`, it walks the main chain down to `48a6bcb8bba1` and drops the content-filter tables, `usage_epoch` and the subscription accesses.
- `alembic downgrade -1` with two heads. It is ambiguous.

What the official rollback discards, and what it keeps:
- Discarded:
  - the named lock table (`named_locks`, from `e5a9c3f1b7d2`);
  - admin passkeys: `admin_passkeys` and `passkey_challenges` are dropped, and the built-in roles lose `admins.passkeys`;
  - pending user resync markers (`pending_user_resyncs`) and node stat acks (`node_stat_acks`). After the rollback image starts, check the nodes' user counts and resync any node that disagrees;
  - the proxy credential claims (`proxy_secret_claims`): v5.34.24 again accepts a credential another user holds or held. The credentials v5.34.25's migration rotated for users who shared one stay rotated, and the old shared values are no longer refused;
  - the durable node removals of deleted users (`pending_node_removals`): a removal the node process had not delivered yet is dropped, so after the rollback image starts, check the nodes' user counts and resync any node that disagrees;
  - on SQLite only, `users` and `nodes` are rebuilt without AUTOINCREMENT (`fd192e9628a5`), so v5.34.24 may again give a new user or node the id of the newest deleted one.
- Kept: the content-filter tables and all their rows. The data fixes of v5.34.25 also stay, because their downgrades are no-ops: VLESS flows no core accepts stay cleared, WireGuard keepalive stays clamped to 65535, a negative `hwid_limit` stays 2147483647, `auto_delete_in_days` stays capped at 36500, and the seeded system Xray client template keeps its socks and http listeners on 127.0.0.1 (upstream #879's migration `bfd88026565b`, whose downgrade is a no-op upstream too). On PostgreSQL, the jsonb columns go back to json.
- Links: v5.34.25's `v4` links answer 404 and pre-upgrade `v3` links keep working, as described at the top of this section. Two revokes of one user within the same second behave as they did on v5.34.24; v5.34.25 fixed that case with the revocation epoch.

Roll forward by deploying v5.34.25 again. Its start.sh upgrades from `d8b2c4f60a17` to head.

### Advanced rollback: keep v5.34.25's links alive
Use this only if v5.34.25's `v4` links must keep opening after the rollback. It needs a rollback image that production has never run, built from branch `hotfix/v5-prod-accept-v4` (d46a03b65 plus `v4` link acceptance), and that image must be built, pushed and proven on the test server first. None existed on 2026-10-07.

Follow steps 1 to 7 above with `--to e5a9c3f1b7d2` in place of `--to d8b2c4f60a17`:
- the dry run lists the same revisions without `e5a9c3f1b7d2`, so `named_locks` is kept;
- the last step prints `done: the database is at e5a9c3f1b7d2`, and `alembic current` prints `e5a9c3f1b7d2 (branchpoint)` under the v5.34.25 image and `e5a9c3f1b7d2 (head)` under the rollback image;
- pin the `hotfix/v5-prod-accept-v4` image in step 6, not v5.34.24-usage-integrity. The v5.34.24-usage-integrity image cannot start on `e5a9c3f1b7d2`: its start.sh fails with `Can't locate revision`;
- on that image a `v4` link keeps opening until the user's next revoke, and the image mints `v3` links again.

`tests/test_migration_rollback_to_prod.py` proves both rollbacks. For the official one, it rolls back from head to `d8b2c4f60a17`, compares schema and rows with a database that never left `d8b2c4f60a17`, checks that the production image's migration graph (tree 72ad0e4a5) is exactly the lineage of `d8b2c4f60a17` here, and runs that tree's `alembic upgrade head` against the result: refused before the rollback, clean after it. For the advanced one, it does the same against `e5a9c3f1b7d2` and the d46a03b65 tree, and also from the two-head state.
