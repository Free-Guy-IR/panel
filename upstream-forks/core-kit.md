# Upstream ledger: core-kit

Upstream commits of PasarGuard/core-kit that Free-Guy-IR/core-kit does not carry as an ancestor, a patch-identical copy
or a cherry-pick or `Upstream:` trailer on its main branch, each with a decision. Checked by
`scripts/upstream_forks.py` from the panel's upstream-inventory workflow. Format and rules are the
same as the panel's UPSTREAM_SYNC.md, including `until: YYYY-MM-DD` deadlines.

Checked on 2026-11-03: Free-Guy-IR/core-kit main is a9cb5ce811aa42b0eff932737373fd8e99b0ceca (the fork's 0.3.4) and
upstream PasarGuard/core-kit main is a6d8d62257ace5d945daf4907bce9a41682085bd (0.3.0, upstream's only branch; git
ls-remote of both repos the same day). The fork carries upstream 0.2.10, 0.2.11 and 0.2.12 in its own 0.3.2, 0.3.3 and
0.3.4 releases. Upstream 0.3.0, released after the fork's 0.3.4, is owed and listed in the port queue.

## Ported by design

- `583561c75e5355f7ad6639991d3754bd4adbf8be` Carried by fork 159cd5d1748a369568d2cef98c07f1fdfe480046 (release 0.3.2), which moves @pasarguard/xray-config-kit from 0.3.9 to 0.3.10 in package.json and bun.lock with the integrity published on npm. Not carried as written: the version string 0.2.10, because the fork numbers its own releases (0.3.1 to 0.3.2), and the bun.lock context lines, because the fork resolves its sibling kits as file: dependencies.
- `b384e36ce9dbcd9333ba0c53b230db5a2223bfb6` Carried by fork 4c89f1d833b6fc6ba63b4bef0c830f3467da3c54 (release 0.3.3), which moves @pasarguard/xray-config-kit to 0.4.0 in package.json and bun.lock and adds the same prepack script. Not carried as written: the version string 0.2.11 (the fork's line is 0.3.3), the bun.lock context lines (file: sibling kits), and the trailing newline change in package.json.
- `d44a7d1645c5605a0b9b1dbe06d062db599254ba` Carried by fork a9cb5ce811aa42b0eff932737373fd8e99b0ceca (release 0.3.4), which moves @pasarguard/xray-config-kit to 0.4.1 in package.json and bun.lock. Not carried as written: the version string 0.2.12 (the fork's line is 0.3.4), the bun.lock context lines (file: sibling kits), and the removal of the trailing newline in package.json.

## Waived

## Port queue

- `a6d8d62257ace5d945daf4907bce9a41682085bd` security: no, upstream core-kit 0.3.0 only moves @pasarguard/xray-config-kit from 0.4.1 to 0.4.2, whose upstream commits eb3cc64 and 26a21e4 add Xray v26.10.31 parity data and drop pre-release versions from that data; fork main still pins 0.4.1. Owner PORT-B-DASH, which holds the dashboard's npm kit waiver, due 2026-11-20 (the dashboard pins @pasarguard/xray-config-kit ^0.3.8). until: 2026-11-20
