# Upstream ledger: subscription-template

Upstream commits of PasarGuard/subscription-template that Free-Guy-IR/subscription-template does not carry as an ancestor, a patch-identical copy
or a cherry-pick or `Upstream:` trailer on its main branch, each with a decision. Checked by
`scripts/upstream_forks.py` from the panel's upstream-inventory workflow. Format and rules are the
same as the panel's UPSTREAM_SYNC.md, including `until: YYYY-MM-DD` deadlines.

## Ported by design

- `39806e40dab668ce7a4e1aaa5e00aee41168473d` ALREADY-IN-FORK: This is the upstream initial commit. Its content is inside fork root commit 4da18eee49eda0d1a2c426ba3913b3f1034a084a, whose tree (4dcd1b32) is byte-identical to upstream 8965004, the cumulative result of this commit and the nine that follow it.
- `99f049544dc893154d2ff4338d76e98cccb58a6e` ALREADY-IN-FORK: The single-file build change is contained in fork root 4da18eee49eda0d1a2c426ba3913b3f1034a084a, whose tree equals upstream 8965004, a descendant of this commit.
- `c2f8b92527e5514e27865d065842cf8e5261d13e` ALREADY-IN-FORK: The version 1.0.0 bump (`package.json`) is contained in fork root 4da18eee49eda0d1a2c426ba3913b3f1034a084a (tree identical to upstream 8965004), and the fork later took the patch-identical 1.0.1/1.0.2 bumps 92c89d1/466f37d.
- `025938ee0124523483df0c6419fd3a56dd7a1355` ALREADY-IN-FORK: This README edit is contained in fork root 4da18eee49eda0d1a2c426ba3913b3f1034a084a (tree identical to upstream 8965004). The fork later rewrote the README on purpose (892b267, 33107f1).
- `e5cd5b6795a5b0ecc6ce2bc7f1f127b4036fc669` ALREADY-IN-FORK: This README edit is contained in fork root 4da18eee49eda0d1a2c426ba3913b3f1034a084a (tree identical to upstream 8965004).
- `92efdaec87beba4c9e2d51a7799d40b1c8017d5c` ALREADY-IN-FORK: This README edit is contained in fork root 4da18eee49eda0d1a2c426ba3913b3f1034a084a (tree identical to upstream 8965004).
- `21596ac6611cf040dd33ddb10fa31ee6e9718973` ALREADY-IN-FORK: The screenshots (`screenshots/en.png`, `screenshots/fa.png`) are contained in fork root 4da18eee49eda0d1a2c426ba3913b3f1034a084a (tree identical to upstream 8965004).
- `fb7a3b77ecc07b0197b6b86f288234d494fa62db` ALREADY-IN-FORK: This README edit, which reached upstream through merge 15e253b, is contained in fork root 4da18eee49eda0d1a2c426ba3913b3f1034a084a (tree identical to upstream 8965004, which descends from that merge).
- `918d1c145c879e6e78de2c43eb1a14f66d422015` ALREADY-IN-FORK: The centered footer (`src/App.tsx`, `src/components/layout/footer.tsx`) is contained in fork root 4da18eee49eda0d1a2c426ba3913b3f1034a084a (tree identical to upstream 8965004).
- `8965004daba32c2dfb9491fff9c764cfaafab367` ALREADY-IN-FORK: Fork root commit 4da18eee49eda0d1a2c426ba3913b3f1034a084a is this commit re-rooted: same author, same message "make default language to persian", and the identical tree 4dcd1b3229d89a5e43523bacf1129ceeaa17203c.

## Waived

