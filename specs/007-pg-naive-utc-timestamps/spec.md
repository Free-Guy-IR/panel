# Feature Specification: PostgreSQL reads naive datetimes as process-local time (F-PG-NAIVE-UTC)

**Feature Branch**: `fix/data-20261111` (spec directory `specs/007-pg-naive-utc-timestamps`)

**Created**: 2026-10-14 as `specs/004-pg-naive-utc-timestamps` on `fix/pg-naive-utc-filter`. Renumbered 2026-11-11:
`004` is taken by `specs/004-certifi-emsign-distrust` and `006` by `specs/006-proxy-url-yarl` on
`fix/proxy-url-yarl`.

**Status**: Implemented on `fix/data-20261111`

**Input**: Finding F-PG-NAIVE-UTC. On PostgreSQL and TimescaleDB, a naive datetime the panel binds to a
`timestamp with time zone` parameter is read by the asyncpg driver as the panel process's local time instead of
UTC. The panel's convention is that a naive datetime is UTC. Usage windows, chart windows and the deleting paths
are shifted by the process's UTC offset whenever the process does not run in UTC.

## Measured ground

Measured 2026-11-11 on the chain tip 8b2249a18 with PostgreSQL 16.2 (server `TimeZone` UTC), asyncpg 0.32.0 and
SQLAlchemy 2.1.6, with the tests of the test-only commit of `fix/data-20261111`.

- `app/db/crud/general.py::to_utc_for_filter` returns a naive UTC datetime. `git grep` finds 30 call sites in
  `app/`, among them the fork's node usage retention cutoff in `app/fork/jobs/cleanup_node_usages.py`.
- A bind parameter typed `DateTime(timezone=True)` renders as `$n::TIMESTAMP WITH TIME ZONE`, so asyncpg encodes
  it with its built-in `timestamptz_encode`, which runs `obj.astimezone(utc)` and reads a naive value as
  process-local time. 2026-10-17 12:00 sent naive reads back as epoch 1792225800 with the process in Asia/Tehran
  (3h30 earlier) and 1792252800 in America/New_York (4 h later), instead of 1792238400. In UTC it is correct.
- A naive value bound through `text()` is typed `DateTime()` and renders as `$n::TIMESTAMP WITHOUT TIME ZONE`, so
  it never reaches the timestamptz encoder and a test written that way cannot fail on a UTC server. The test
  that bound through `text()` was dropped for that reason and replaced by the typed bind above.
- `cleanup_node_usages` with a 7-day retention: in America/New_York and America/Los_Angeles it deletes both rows
  that are still inside the retention period; in Asia/Tehran it keeps the row that is one hour past it.
- `get_users_subscription_agent_counts` for the current hour returns nothing in Asia/Tehran and
  America/New_York, and this hour's row in UTC.
- On SQLite the same tests pass before and after the fix.
- Upstream: `to_utc_for_filter` still returns naive UTC on upstream `dev` c28cbf1cd (2026-11-10) and on tag
  v5.6.0, and `app/` there has no timestamptz codec. No upstream fix exists.

## Reach

- Direction: with a process zone east of UTC a window lands in the past; west of UTC it lands in the future, so a
  retention cutoff deletes rows that are still inside the retention period.
- `send_notifications.delete_expired_reminders` binds a naive `now` too, but on 8b2249a18 it still never commits
  (`GetDB` does not commit), so it deletes nothing on any backend. Upstream release v5.5.1 adds the commit
  (#947). Once that lands, this fix covers its cutoff as well.

## Decision

Fix the binding, not the callers. On every asyncpg connection, timestamptz values are encoded with a codec that
reads a naive datetime (and a bare date) as UTC, which is the panel-wide convention and what the other backends
already do. Aware datetimes are encoded exactly as before. Decoding is exactly asyncpg's built-in result: aware
UTC, or the naive `datetime.max` / `datetime.min` for +/-infinity.

The codec is registered once, as a SQLAlchemy `Pool` "connect" listener, from the fork package (`app/fork`), which
`app/db/models.py` imports before any connection exists. No upstream file changes. MySQL, MariaDB and SQLite
connections are not touched.

Rejected:
- Making `to_utc_for_filter` return aware UTC. It changes an upstream function and leaves its upstream docstring
  untrue, it misses the other naive binds, and under a non-UTC process it would keep failing the fork tests that
  seed naive `created_at` values (`tests/test_clear_usage_data_window.py`).
- Changing each caller: 30 call sites, most of them in upstream files, and every new naive bind would bring the
  bug back.

## User Scenarios & Testing

### User Story 1 - Retention never deletes rows inside the retention period (Priority: P1)

An operator runs the panel on PostgreSQL in a process whose zone is west of UTC. The node usage retention job
deletes only rows older than the retention boundary.

**Independent Test**: Seed node usage rows 1 hour and 20 minutes inside the boundary and 1 hour outside it. Set
the process TZ to UTC, Asia/Tehran, America/New_York and America/Los_Angeles and run `cleanup_node_usages`. The
inside rows remain and the outside row is gone.

### User Story 2 - Usage and subscription windows cover the requested instants (Priority: P1)

A dashboard asks for this hour's subscription-agent counts. Under any process zone the row written this hour is
counted.

**Independent Test**: Write one subscription update now, set the process TZ to UTC, Asia/Tehran and
America/New_York, and ask `get_users_subscription_agent_counts` for the current hour.

### User Story 3 - A naive value bound to a timestamptz parameter means UTC (Priority: P1)

**Independent Test**: Bind the same instant naive and aware to `DateTime(timezone=True)` parameters on
PostgreSQL under UTC, Asia/Tehran and America/New_York; both must read back as the same epoch.

### User Story 4 - Nothing changes on MySQL, MariaDB and SQLite (Priority: P1)

The same tests run on every backend in CI and pass on MySQL, MariaDB and SQLite before and after the fix.

### Edge Cases

- +/-infinity timestamps round-trip exactly as with asyncpg's built-in codec.
- A bare `date` bound to timestamptz is read as UTC midnight, no longer as local midnight.
- Aware values in any zone are stored as the same instant as before.
- Microsecond precision is kept, and values before 2000-01-01 (negative offsets from the PostgreSQL epoch)
  round-trip.

## Requirements

- **FR-001**: On PostgreSQL and TimescaleDB, a naive datetime bound to a timestamptz parameter MUST be stored and
  compared as that wall time in UTC, whatever the process TZ.
- **FR-002**: Aware datetimes MUST bind to the same instant as before. Decoded values MUST be identical to
  asyncpg's built-in decoding.
- **FR-003**: MySQL, MariaDB and SQLite connections MUST NOT be altered.
- **FR-004**: Installation MUST fail loudly. If the codec cannot be set on an asyncpg connection, that connection
  fails.
- **FR-005**: Every new behaviour test MUST be RED on the parent of the fix under PostgreSQL with a non-UTC
  process TZ, and GREEN on the fix on every backend.

## Success Criteria

- **SC-001**: `cleanup_node_usages` under America/Los_Angeles and America/New_York deletes 0 rows newer than the
  retention boundary on PostgreSQL.
- **SC-002**: The window and bind tests pass for UTC, Asia/Tehran and America/New_York on PostgreSQL.
- **SC-003**: `tests/api/test_subscription_update_snapshot.py` passes on PostgreSQL under an Asia/Tehran process.
- **SC-004**: Identical results before and after on MySQL, MariaDB and SQLite.

## Out of scope

- Sync psycopg connections. In production, Alembic online migrations run through asyncpg: `start.sh` runs
  `alembic upgrade head`, and `app/db/migrations/env.py` migrates with `run_async_migrations` on the panel's
  async URL, so migrations go through this codec. Only the test harness (`tests/api/__init__.py`) migrates through
  psycopg, where a naive value is sent as `timestamp` and converted with the server's own `TimeZone`.
- Submitting upstream. This is recorded as an upstream-submission candidate with its RED test, to be bundled under
  the submission rule.
