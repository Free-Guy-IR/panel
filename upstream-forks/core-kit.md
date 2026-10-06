# Upstream ledger: core-kit

Upstream commits of PasarGuard/core-kit that Free-Guy-IR/core-kit does not carry as an ancestor, a patch-identical copy
or a cherry-pick or `Upstream:` trailer on its main branch, each with a decision. Checked by
`scripts/upstream_forks.py` from the panel's upstream-inventory workflow. Format and rules are the
same as the panel's UPSTREAM_SYNC.md, including `until: YYYY-MM-DD` deadlines.

## Ported by design


## Waived

- `d01cc1634499b89185048621459f35d910efbf66` WAIVED: The commit changes only `bun.lock` (the xray-config-kit 0.3.7 lock entry). The fork deliberately stopped committing `bun.lock` in 536a7b1457ac09b13c8ae90684696f46473c9ba2 ("Stop committing bun.lock so consumers can resolve the sibling kits"), and fork main has no `bun.lock`.
- `6773b0dbbb7b7f00f0d0cd132b11a87d43533779` WAIVED: The commit moves the xray-config-kit pin from 0.3.7 to 0.3.8 in `package.json`/`bun.lock`. Upstream xray-config-kit 0.3.8 (a5851cc) differs from 0.3.7 (6a11d38) only in its own `package.json` version string (`git diff 6a11d38 a5851cc` touches one line), so the fork's 0.3.7 pin is byte-identical in behaviour, and the fork keeps no lockfile.
- `4d5de5de56af5486d9dc43d596c814f20bcd8677` WAIVED: The commit bumps the core-kit version from 0.2.7 to 0.2.8 and changes nothing else. The fork core-kit is on its own version line, `"version": "0.3.0"` at fork main `package.json:3`, and the only content between upstream 0.2.7 and 0.2.8 is the two dependency-lock commits waived above.

## Port queue

- `d2ecb5bd8afcc57b58813a556b4d486fdb2ac96b` PORT-NEEDED: This is upstream 0.2.9, which moves @pasarguard/xray-config-kit from 0.3.8 to 0.3.9 (Xray-core v26.9.8/v26.9.9 capability sets and quicParams fields) and bumps upstream's own version from 0.2.8 to 0.2.9 in `package.json` and `bun.lock`. The owner's decision is to take the 0.3.9 pin, while the 0.2.9 version string stays superseded by the fork's 0.3.x line. Re-verified 2026-10-06: live fork main is still edd61852708e (`git ls-remote`), its `package.json` still says version 0.3.0 with `"@pasarguard/xray-config-kit": "0.3.7"`, and the commit is not its ancestor. The port exists only on the local, unpushed branch `update/2026-09-24` in `~/Projects/pasarguard/work/update-2026-09-24/core-kit`, head f245986eaae35d6bc75a5a8b021fcd074eb1171d, 12 commits ahead of fork main and fast-forwarding it: merge commit 81e6713709815672d47e732c84a868f1d45a146f takes upstream main d2ecb5b as its second parent (the 0.3.9 pin, keeping the fork version line and the file: sibling kits), and 28d8bd264ad56fdf7bd87ac84226536b5435c776 then releases it as core-kit 0.3.1. GitHub serves none of the three commits (`upload-pack: not our ref`); the push waits on the 3/3 review gate, whose last run on f245986 asked for changes. Once f245986 is on fork main, this commit and the three waived above become ancestors of the base, and all four lines are deleted; a dry run of this gate on 2026-10-06 with f245986 standing in as fork main found 0 upstream commits past the base and reported exactly these four lines as stale. It replaces the bridge waiver that expired on 2026-10-02. until: 2026-10-20
