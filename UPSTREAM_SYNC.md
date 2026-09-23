# Upstream Sync Ledger

This file records every upstream PasarGuard commit that this fork has not cherry-picked
but has dealt with anyway. The upstream parity gate in `.github/workflows/upstream-inventory.yml`
reads it through `scripts/upstream_inventory.py --fail-on-unported UPSTREAM_SYNC.md` and fails when
an upstream commit past the pinned baseline, on any upstream branch or tag, is neither ported
nor listed here.

A commit ported with `git cherry-pick -x` needs no entry: the `(cherry picked from commit <sha>)`
trailer in the fork history covers it automatically, and so does any rebased or duplicated copy of
a covered commit with the same `git patch-id --stable`.

Everything else goes under one of the two headings below, one list item per commit:

    - `<sha>` <reason>

Use the full 40-character sha. A prefix of at least 9 hex characters is accepted while it names
exactly one upstream commit in range, but it becomes a ledger error once that commit leaves the
range. An entry without a reason fails the gate.

`Ported by design` is for upstream changes the fork carries in a different form (adapted by hand,
merged into a fork commit, or superseded by fork code). `Waived` is for upstream changes the fork
deliberately does not take.

## Ported by design

## Waived
