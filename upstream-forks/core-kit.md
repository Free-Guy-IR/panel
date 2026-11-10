# Upstream ledger: core-kit

Upstream commits of PasarGuard/core-kit that Free-Guy-IR/core-kit does not carry as an ancestor, a patch-identical copy
or a cherry-pick or `Upstream:` trailer on its main branch, each with a decision. Checked by
`scripts/upstream_forks.py` from the panel's upstream-inventory workflow. Format and rules are the
same as the panel's UPSTREAM_SYNC.md, including `until: YYYY-MM-DD` deadlines.

Checked on 2026-11-10: Free-Guy-IR/core-kit main is 99566c40b04d2a8b18bc1e955e532e1e093ceca1 (the fork's 0.3.5,
pinning @pasarguard/xray-config-kit 0.4.2) and upstream PasarGuard/core-kit main is
a6d8d62257ace5d945daf4907bce9a41682085bd (0.3.0, upstream's only branch). The earlier head recorded here,
a9cb5ce811aa42b0eff932737373fd8e99b0ceca, is the fork's 0.3.4 pinning 0.4.1 and is now an ancestor of fork main. The
fork carries upstream 0.2.10, 0.2.11 and 0.2.12 in its own 0.3.2, 0.3.3 and 0.3.4 releases and upstream 0.3.0 in its
0.3.5, so no upstream commit is owed.

The owner rule of 2026-10-14 (port only final upstream releases) applies to this repository. core-kit publishes no
GitHub Release, but it is published on npm: on 2026-11-10 registry.npmjs.org/@pasarguard/core-kit lists 21 versions
with `dist-tags.latest` 0.3.0, published 2026-11-01. A verified stable npm publication, or the exact upstream commit
that publication was built from, is therefore the eligible source for a port here; an unpublished upstream main commit
is not.

## Ported by design

- `583561c75e5355f7ad6639991d3754bd4adbf8be` Carried by fork 159cd5d1748a369568d2cef98c07f1fdfe480046 (release 0.3.2), which moves @pasarguard/xray-config-kit from 0.3.9 to 0.3.10 in package.json and bun.lock with the integrity published on npm. Not carried as written: the version string 0.2.10, because the fork numbers its own releases (0.3.1 to 0.3.2), and the bun.lock context lines, because the fork resolves its sibling kits as file: dependencies.
- `b384e36ce9dbcd9333ba0c53b230db5a2223bfb6` Carried by fork 4c89f1d833b6fc6ba63b4bef0c830f3467da3c54 (release 0.3.3), which moves @pasarguard/xray-config-kit to 0.4.0 in package.json and bun.lock and adds the same prepack script. Not carried as written: the version string 0.2.11 (the fork's line is 0.3.3), the bun.lock context lines (file: sibling kits), and the trailing newline change in package.json.
- `d44a7d1645c5605a0b9b1dbe06d062db599254ba` Carried by fork a9cb5ce811aa42b0eff932737373fd8e99b0ceca (release 0.3.4), which moves @pasarguard/xray-config-kit to 0.4.1 in package.json and bun.lock. Not carried as written: the version string 0.2.12 (the fork's line is 0.3.4), the bun.lock context lines (file: sibling kits), and the removal of the trailing newline in package.json.
- `a6d8d62257ace5d945daf4907bce9a41682085bd` Carried by fork 99566c40b04d2a8b18bc1e955e532e1e093ceca1 (release 0.3.5), which carries the cherry-pick and `Upstream: a6d8d62257ac` trailers for it. Upstream 0.3.0 changes only bun.lock and package.json, and its whole substance is the move of @pasarguard/xray-config-kit from 0.4.1 to 0.4.2: the fork's package.json pins `"@pasarguard/xray-config-kit": "0.4.2"` and its bun.lock resolves `@pasarguard/xray-config-kit@0.4.2` with the same integrity `sha512-v0XkCLQWx4eZV7jkUXSUiQleDvwkQ7c7urmmvtQVFbm4stWirnGxYh0mznl0xKgETYkDKRG7uG/1AnLBD3wMFw==` that upstream's own bun.lock records, which is the hash npm publishes for 0.4.2. The panel consumes it at that version too: `.github/config-kits.lock` pins core-kit 99566c40b04d2a8b18bc1e955e532e1e093ceca1 and dashboard/bun.lock resolves xray-config-kit 0.4.2 with the same integrity, both directly and nested under @pasarguard/core-kit. Not carried as written, so all four of its hunks are flagged: the version string 0.3.0, because the fork numbers its own releases (0.3.4 to 0.3.5); the bun.lock dependency block and the bun.lock entry context, because the fork resolves its sibling kits as file: dependencies and so has five more of them in that block; and the package.json dependency block for the same reason. The fork commit also updates tests/xray-config-kit.test.ts for the 0.4.2 parity data, which upstream 0.3.0 does not touch. security: no, a dependency version bump: xray-config-kit 0.4.2 adds Xray v26.10.31 parity data, drops the pre-release tags from that data, and adds the XDrive s3 service, xhttp h2Params and two TUN fields; an api.osv.dev query for @pasarguard/xray-config-kit 0.4.1 on 2026-11-10 returned no advisory.

## Waived

## Port queue
