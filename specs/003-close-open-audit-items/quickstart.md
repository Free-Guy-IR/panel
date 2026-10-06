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

Before v5.34.25 goes live, build and push the rollback image from branch `hotfix/v5-prod-accept-v4` (production d46a03b65 plus v4 link acceptance) and record its tag next to the v5.34.25 tag. v5.34.25 is not deployed while no rollback image exists.

## 8. Rollback to the v5.34.24 line (main session only)
The rollback target is the image built from `hotfix/v5-prod-accept-v4`. Never roll back to bare d46a03b65 or the v5.34.24-usage-integrity image: v5.34.25 mints `v4,<id>,<ts>,<epoch>` subscription links, bare v5.34.24 answers every one of them with 404, and the rollback image accepts them with the revocation rule v5.34.25 enforces.

The database goes back to exactly `e5a9c3f1b7d2`, the v5.34.24 head, through one entry point that ships in the v5.34.25 image. Run every step with the v5.34.25 image still selected, because only that image carries the downgrades.

1. Back up the database (mysqldump, pg_dump, or a copy of the SQLite file) and note the running v5.34.25 image tag.
2. Stop the panel and every other container that writes to the database: `docker compose stop pasarguard`.
3. Preview: `docker compose run --rm --no-deps --entrypoint python pasarguard -m app.fork.db_rollback --to e5a9c3f1b7d2 --dry-run`
   It must print `revisions to remove: 009f2a24cc7b, 0374ac83604b, 6698b9a695e3, 6d8eb8fba21b, 8158387867c9, 9a5c2e7f1b4d, b176d23e434c, b4c7e8f1a2d3, bfd88026565b, e79833e2a0b2, e9a7edd9de06, f7c63e209a37, fd192e9628a5, fd33c6013baa`.
   The entry point reads the database URL from the image's environment. A URL passed with `--url` must use the async driver the panel itself uses (`sqlite+aiosqlite://`, `mysql+asyncmy://` or `postgresql+asyncpg://`); a sync URL such as `sqlite:///` fails before anything is changed.
4. Roll back: `docker compose run --rm --no-deps --entrypoint python pasarguard -m app.fork.db_rollback --to e5a9c3f1b7d2`
   Expected steps, in this order: `alembic downgrade e5a9c3f1b7d2`, `alembic downgrade b4c7e8f1a2d3@-1`, `alembic downgrade 9a5c2e7f1b4d@-1`, then `done: the database is at e5a9c3f1b7d2`. Exit code 0 is success. Exit code 2 prints `rollback refused: ...`, and any other failure exits non-zero with a traceback. On either, the database stays at the last step printed. Rerun step 4 once the cause is fixed, or restore the backup from step 1.
5. Check: `docker compose run --rm --no-deps --entrypoint python pasarguard -m alembic current` must print exactly one revision line: `e5a9c3f1b7d2 (branchpoint)` under the v5.34.25 image. Under the rollback image, the same line reads `e5a9c3f1b7d2 (head)`.
6. Pin the rollback image in docker-compose.override.yml and `docker compose up -d`. Its start.sh runs `alembic upgrade head`, which is a no-op at `e5a9c3f1b7d2`.
7. Verify in a real browser that the dashboard renders. Check that a subscription link v5.34.25 handed out still opens (`/sub/<token>/info` gives 200), and that a test user's old link gives 404 after a revoke.

Never run these commands:
- `alembic downgrade e5a9c3f1b7d2` on its own. It removes only the descendants of `e5a9c3f1b7d2`. The upstream passkey branch (`9a5c2e7f1b4d`, `b4c7e8f1a2d3`) forks off `48a6bcb8bba1` and would stay in alembic_version, and the v5.34.24 image then crash-loops on `Can't locate revision identified by 'b4c7e8f1a2d3'`.
  - The fork's merge revision `fd33c6013baa` now refuses that downgrade unless the entry point runs it. The plain command exits 1 with `Refusing to downgrade fd33c6013baa on its own` and leaves the database at `fd33c6013baa`. The main chain above it is already gone at that point, so the v5.34.24 image cannot start on it either.
  - Run step 4 from there. The entry point also repairs the two-head state `b4c7e8f1a2d3, e5a9c3f1b7d2`, which an earlier v5.34.25 build without the refusal could leave.
  - Every other path that would remove `fd33c6013baa` is refused the same way, and step 4 repairs each of them to exactly `e5a9c3f1b7d2`:
    - `alembic downgrade 48a6bcb8bba1` and `alembic downgrade base` stop at `fd33c6013baa`;
    - `alembic downgrade 9a5c2e7f1b4d` stops at two heads, `0374ac83604b, fd33c6013baa`. The fork chain above `e5a9c3f1b7d2` is not a descendant of `9a5c2e7f1b4d`, so that downgrade leaves it in place.
- `alembic downgrade b4c7e8f1a2d3-2`. Without `@`, it walks the main chain down to `48a6bcb8bba1` and drops the content-filter tables, `usage_epoch` and the subscription accesses.
- `alembic downgrade -1` with two heads. It is ambiguous.

What the rollback discards, and what it keeps:
- Discarded:
  - admin passkeys: `admin_passkeys` and `passkey_challenges` are dropped, and the built-in roles lose `admins.passkeys`;
  - pending user resync markers (`pending_user_resyncs`) and node stat acks (`node_stat_acks`). After the rollback image starts, check the nodes' user counts and resync any node that disagrees;
  - the proxy credential claims (`proxy_secret_claims`): v5.34.24 again accepts a credential another user holds or held. The credentials v5.34.25's migration rotated for users who shared one stay rotated, and the old shared values are no longer refused.
  - the durable node removals of deleted users (`pending_node_removals`): a removal the node process had not delivered yet is dropped, so after the rollback image starts, check the nodes' user counts and resync any node that disagrees;
  - on SQLite only, `users` and `nodes` are rebuilt without AUTOINCREMENT (`fd192e9628a5`), so v5.34.24 may again give a new user or node the id of the newest deleted one.
- Kept: the content-filter tables and all their rows. The data fixes of v5.34.25 also stay, because their downgrades are no-ops: VLESS flows no core accepts stay cleared, WireGuard keepalive stays clamped to 65535, a negative `hwid_limit` stays 2147483647, `auto_delete_in_days` stays capped at 36500, and the seeded system Xray client template keeps its socks and http listeners on 127.0.0.1 (upstream #879's migration `bfd88026565b`, whose downgrade is a no-op upstream too). On PostgreSQL, the jsonb columns go back to json.
- Links:
  - A v4 link keeps opening on the rollback image until the user's next revoke.
  - The rollback image mints v3 links again.
  - Two revokes of one user within the same second behave as they did on v5.34.24. v5.34.25 fixed that case with the revocation epoch.

Roll forward by deploying v5.34.25 again. Its start.sh upgrades from `e5a9c3f1b7d2` to head.

`tests/test_migration_rollback_to_prod.py` proves this procedure. It rolls back from head and from the two-head state and compares schema and rows with a database that never left `e5a9c3f1b7d2`. It then runs the real d46a03b65 tree's `alembic upgrade head` against the result.
