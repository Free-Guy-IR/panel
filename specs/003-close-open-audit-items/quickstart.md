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
