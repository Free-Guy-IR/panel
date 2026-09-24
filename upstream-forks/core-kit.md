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
- `d2ecb5bd8afcc57b58813a556b4d486fdb2ac96b` WAIVED until: 2026-10-25: The version bump to 0.2.9 is superseded by the fork's own 0.3.0 line. The only functional part is the xray-config-kit pin moving to 0.3.9 (upstream e33fe3a: Xray-core v26.9.8/v26.9.9 capability sets and new quicParams fields). Inside core-kit, that pin only changes `validateStrictXrayConfig`/`createDefaultXrayCoreConfigJson` (fork `src/xray-kit.ts`). `verifyPeerCertByName` is typed `"string"` in the parity manifests of both 0.3.7 and 0.3.9, and the fork dashboard already carries upstream panel 31142a590's `verifyPeerCertByName` coercion in `sanitize-inbound.ts`/`outbound-editor-json.ts`. The fork's nodes run Xray-core v26.3.27 (node fork main 2d8e11f `Dockerfile:63` `ARG XRAY_VERSION=v26.3.27`; upstream nodes also resolve to v26.3.27 because v26.6.27..v26.9.9 are prereleases), which is already older than the v26.7.28 capability set of the pinned 0.3.7. Validating against v26.9.x would only admit fields the fork's cores do not implement. Revisit trigger: take the pin as soon as the fork node's `XRAY_VERSION` moves to v26.9.8 or later.
