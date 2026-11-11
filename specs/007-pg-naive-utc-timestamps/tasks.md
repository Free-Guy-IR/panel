# Tasks: PostgreSQL reads naive datetimes as process-local time (F-PG-NAIVE-UTC)

- [x] T001 Check upstream for a change to `to_utc_for_filter` or a timestamptz codec. Result on `dev` c28cbf1cd and
      tag v5.6.0: none.
- [x] T002 Write the spec, the plan and this task list (first as `specs/004-pg-naive-utc-timestamps`, renumbered
      to 007 on 2026-11-11).
- [x] T003 Test-only commit: `tests/test_naive_utc_process_timezone.py` covering the retention cutoff, the
      subscription-agent window and the typed timestamptz bind.
- [x] T004 Run T003 on the chain tip 8b2249a18 on SQLite and PostgreSQL 16.2. RED on PostgreSQL only: retention
      under Asia/Tehran, America/New_York and America/Los_Angeles, the window and the bind under Asia/Tehran and
      America/New_York.
- [x] T005 Add `app/fork/naive_utc_timestamps.py` and the call from `app/fork/__init__.py`.
- [x] T006 Add `tests/test_naive_utc_timestamps_codec.py`: codec properties, decode parity with asyncpg's built-in
      decoder, and the registration guard. Drop the `text()`-bound engine test, which cannot fail.
- [x] T007 GREEN locally: T003, T006 and the existing time-window tests on SQLite, and on PostgreSQL 16.2 under
      process TZ UTC, Asia/Tehran and America/New_York.
- [ ] T008 CI: RED run of the test-only commit and GREEN run of the fix on SQLite, PostgreSQL, TimescaleDB, MySQL
      and MariaDB.
- [ ] T009 A non-author verifier re-runs the RED on the final head.
- [x] T010 Record the upstream-submission candidate with its RED test.
