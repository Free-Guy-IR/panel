# Implementation Plan: PostgreSQL reads naive datetimes as process-local time (F-PG-NAIVE-UTC)

**Branch**: `fix/data-20261111` | **Spec**: `specs/007-pg-naive-utc-timestamps/spec.md`

## Summary

Every asyncpg connection gets a `timestamptz` codec in asyncpg's documented `tuple` format. The encoder reads a
naive datetime, or a bare date, as UTC; it encodes an aware datetime to the same instant as before; and it keeps
the +/-infinity sentinels. The decoder returns exactly what asyncpg's built-in decoder returns. The codec is
installed by a SQLAlchemy `Pool` "connect" listener that only acts when the driver connection is an asyncpg
connection.

## Technical Context

Python 3.14, SQLAlchemy 2.1.6, asyncpg 0.32.0, asyncmy 0.2.17, aiosqlite 0.22.1 (the chain tip's `uv.lock`).
Backends: PostgreSQL and TimescaleDB (asyncpg), MySQL and MariaDB (asyncmy), SQLite (aiosqlite).

## Constitution Check

- Upstream boundary: the codec module and its registration live in `app/fork`. The registration is called from
  `app/fork/__init__.py`, which `app/db/models.py` already imports before any engine connects. No upstream file
  changes. PASS.
- Verified ground: every measured claim in the spec comes from the RED and GREEN runs of 2026-11-11. PASS.
- Fleet safety: no node or core is touched. PASS.
- Clean code: no comments, ruff clean. PASS.
- Review: a non-author verifier re-runs the RED on the final head. PENDING.

## Design

- `app/fork/naive_utc_timestamps.py`:
  - `encode_timestamptz(value) -> tuple[int]` and `decode_timestamptz(value: tuple[int]) -> datetime`.
  - `install_naive_utc_timestamps()` registers `_install_codec` on `Pool` "connect" once. It is idempotent
    through `event.contains`.
  - `_install_codec(dbapi_connection, record)` acts only when `dbapi_connection.driver_connection` is an asyncpg
    `Connection`. It calls `set_type_codec("timestamptz", schema="pg_catalog", format="tuple")` through
    `dbapi_connection.run_async`. Any error propagates, so that connection fails (FR-004).
- `app/fork/__init__.py` calls `install_naive_utc_timestamps()`.

## Equivalence with asyncpg's built-in codec

| Input | Built-in | New |
|---|---|---|
| aware datetime | `astimezone(utc)` | same instant |
| naive datetime | read as process-local | read as UTC |
| date | local midnight | UTC midnight |
| naive `datetime.max` / `datetime.min` | +/-infinity | +/-infinity |
| decode finite | aware UTC | aware UTC |
| decode +/-infinity | naive `datetime.max` / `datetime.min` | same |

## Test strategy

1. Test-only commit, `tests/test_naive_utc_process_timezone.py`, run on every backend:
   - node usage retention under UTC, Asia/Tehran, America/New_York and America/Los_Angeles;
   - this hour's subscription-agent count under UTC, Asia/Tehran and America/New_York;
   - on PostgreSQL, a naive and an aware value bound to `DateTime(timezone=True)` parameters read back as the same
     epoch under UTC, Asia/Tehran and America/New_York.

   Expected RED on PostgreSQL and TimescaleDB for every non-UTC zone, GREEN on SQLite, MySQL and MariaDB before
   and after the fix.
2. Fix commit, `tests/test_naive_utc_timestamps_codec.py`:
   - pure encoder and decoder properties;
   - on PostgreSQL, values read through the panel engine equal the values read through a raw asyncpg connection
     without the codec, for finite, pre-2000, microsecond and +/-infinity values;
   - the registration is idempotent;
   - it leaves a non-asyncpg connection untouched.

   The `text()`-bound engine test of the first version is not kept: it binds a naive value as
   `TIMESTAMP WITHOUT TIME ZONE` and cannot fail on a UTC server.
3. Matrix: SQLite and PostgreSQL 16 locally, PostgreSQL under process TZ UTC, Asia/Tehran and America/New_York,
   and CI on SQLite, PostgreSQL, TimescaleDB, MySQL and MariaDB. Neighbours:
   `tests/api/test_subscription_update_snapshot.py`, `tests/test_clear_usage_data_window.py`,
   `tests/test_retention_node_usages.py` and `tests/test_cleanup_window_timezone.py`.
