# Upstream ledger: scripts

Upstream commits of PasarGuard/scripts that Free-Guy-IR/scripts does not carry as an ancestor, a patch-identical copy
or a cherry-pick or `Upstream:` trailer on its main branch, each with a decision. Checked by
`scripts/upstream_forks.py` from the panel's upstream-inventory workflow. Format and rules are the
same as the panel's UPSTREAM_SYNC.md, including `until: YYYY-MM-DD` deadlines. Reviewed on 2026-10-06 against upstream
`main` 1fce1b19aaa5, upstream `shared_lib` bbb0fc6c4712 and all 13 upstream tags (v0.0.1 to v0.1.9). Fork `main`
4a6ff923ebbe is upstream tag v0.1.9 (5fa4eac7fa78) plus four fork commits (1d2dc9f409c7, 086c0a66ef98, 5c14454a7aab,
4a6ff923ebbe), so every upstream tag is an ancestor of it. The `shared_lib` head bbb0fc6c4712 is patch-identical to
f66623f6af25 on fork main (patch-id ddc98d1d14af) and needs no line. Upstream pull requests are outside this gate,
which tracks branches and tags; the fork's own tracker `parity/ledger.tsv`, so far only on the unpushed local branch
`update/2026-09-24`, records decisions for PR #22, #25, #30, #32 and #33. Re-checked on 2026-10-11 02:55 UTC with
`git ls-remote`: upstream `main` bba95f274216 (PR #35, #36 and #37 squash-merged after #34 7bccaecec5cd), branches
`fix/restore-safety-followups` 0cb8eff84a3b, `fix/backup-metadata-followups` 9c50eb356d45, `fix/fresh-restore-retry`
b22189884ed7 and `shared_lib` bbb0fc6c4712, no tag after v0.1.9; fork `main` is still 4a6ff923ebbe.

## Ported by design

- `0d37ec20d4c57bd79db2856915597b81b7fb5958` ALREADY-IN-FORK: Pre-squash commit of upstream PR #19 on branch `shared_lib`; it creates `lib/common.sh` and `lib/env.sh` and removes the duplicated helpers from `pasarguard.sh` and `pg-node.sh`. Upstream squash-merged the branch as 1a1bbfd3c408 "refactor: Shared lib (#19)", whose parent is the branch's fork point 36bf1a007077 and whose tree equals the branch tree at 6cd7ef38bac4 (`git diff 6cd7ef38bac4 1a1bbfd3c408` is empty). 1a1bbfd3c408 is an ancestor of fork main 4a6ff923ebbe, which has both files.
- `d22afb514967112b0f665e083bf3b2fc940012cd` ALREADY-IN-FORK: Pre-squash commit of upstream PR #19; it splits `lib/docker.sh`, `lib/github.sh` and `lib/system.sh` out of `lib/common.sh`. Its net change is in squash 1a1bbfd3c408 (tree identical to the branch at 6cd7ef38bac4, parent 36bf1a007077), an ancestor of fork main 4a6ff923ebbe, which has all three files.
- `1b9f30ca623a1c737ca375e05afa3a4993d6b956` ALREADY-IN-FORK: Pre-squash commit of upstream PR #19 (temp-file handling in `lib/common.sh`, `lib/env.sh`, `lib/github.sh`, `pg-node.sh` and the README). All 56 lines it adds are in squash 1a1bbfd3c408, an ancestor of fork main 4a6ff923ebbe.
- `273b4d42dc6b48f17a97a6be27f05e096aaf7099` ALREADY-IN-FORK: Pre-squash commit of upstream PR #19 that moves the compose files into `docker-compose/`. The move is in squash 1a1bbfd3c408, an ancestor of fork main 4a6ff923ebbe; fork main has `docker-compose/node.yml` and the `docker-compose/pasarguard-*.yml` files and none of them at the repository root.
- `fdd8513dd6f071690eb8de6ed1636e713eb7a1e8` ALREADY-IN-FORK: Pre-squash commit of upstream PR #19 that moves the backup and restore code out of `pasarguard.sh` into `lib/pasarguard-backup.sh` and `lib/pasarguard-restore.sh`. Its net change is in squash 1a1bbfd3c408 (tree identical to the branch at 6cd7ef38bac4), an ancestor of fork main 4a6ff923ebbe, which has both files.
- `edde1ab9e9c23ca8b33fae0fdb7fdd1ba3abc6e7` ALREADY-IN-FORK: Pre-squash "fix" commit of upstream PR #19 (`lib/docker.sh`, `lib/github.sh`, `lib/system.sh`, `pasarguard.sh`). All 40 lines it adds are in squash 1a1bbfd3c408, an ancestor of fork main 4a6ff923ebbe.
- `a61d3dcdf1335041eeeb64333f33b88b6886d37f` ALREADY-IN-FORK: Pre-squash "fix" commit of upstream PR #19 (backup, restore and `lib/system.sh`). All 50 lines it adds are in squash 1a1bbfd3c408, an ancestor of fork main 4a6ff923ebbe.
- `a021ba5282fa038d7e31036c1a15e0fc675ae324` ALREADY-IN-FORK: Pre-squash "fix: emoji" commit of upstream PR #19 (output strings in backup, restore and `pasarguard.sh`). Its net change is in squash 1a1bbfd3c408 (tree identical to the branch at 6cd7ef38bac4), an ancestor of fork main 4a6ff923ebbe.
- `cd4e86087b34dc9638bbfe6c15ff43ea039f1223` ALREADY-IN-FORK: Pre-squash "fix" commit of upstream PR #19 (`lib/pasarguard-backup.sh`, `lib/pasarguard-restore.sh`). All 74 lines it adds are in squash 1a1bbfd3c408, an ancestor of fork main 4a6ff923ebbe.
- `0a00b91b18c8d7d41442993f10431085e418e79c` ALREADY-IN-FORK: Pre-squash "fix" commit of upstream PR #19 (backup and restore). All 9 lines it adds are in squash 1a1bbfd3c408, an ancestor of fork main 4a6ff923ebbe.
- `702abd2a461006977544b9f7474b469e6ba0f756` ALREADY-IN-FORK: Pre-squash "fix" commit of upstream PR #19 (backup and restore). All 18 lines it adds are in squash 1a1bbfd3c408 and also on fork main 4a6ff923ebbe, which descends from it.
- `6cd7ef38bac4b7e2c068328395a9979d1f5e5382` ALREADY-IN-FORK: "fix: github url" restores `github_raw_url` in `lib/github.sh` to the `raw/main` form, the exact reverse of 3d396c54d912. Its tree is the tree of squash 1a1bbfd3c408, an ancestor of fork main 4a6ff923ebbe, and fork main `lib/github.sh:9` builds `raw/main` URLs.

## Waived

- `3d396c54d91210c04668fadb8a41e4f427dc1b75` WAIVED: "temp: change raw url" pointed `github_raw_url` in `lib/github.sh` at the `shared_lib` branch while PR #19 was being tested. The same branch reversed it exactly in 6cd7ef38bac4, so upstream's squash 1a1bbfd3c408 (tree identical to 6cd7ef38bac4) never carried it and upstream main never had it; fork main `lib/github.sh:9` keeps `raw/main`. The fork's own `parity/ledger.tsv` (unpushed branch `update/2026-09-24`) waives the whole `shared_lib` branch as the pre-merge branch of PR #19. security: no

- `a7417383672da5031d6c352c57d03bfd48c858f7` SUPERSEDED-BY-SQUASH: pre-squash commit of upstream PR #35 on branch fix/restore-safety-followups (refuse cross-engine restores, keep restore data safety fail-closed). The branch tip 0cb8eff84a3b has exactly the tree of squash 8b12ff936f8996fda236d276ffb3bcf2087fcd3c on upstream main (`git diff 0cb8eff84a3b 8b12ff936f89` is empty), so its whole change is decided by that squash's Port queue line. security: no
- `0cb8eff84a3b6cbd7f4217147e17eb75a4f9432c` SUPERSEDED-BY-SQUASH: pre-squash commit of upstream PR #35 that names the engine family in the Farsi cross-engine refusal (docs/backup-and-restore.fa.md); the branch tip's tree equals squash 8b12ff936f8996fda236d276ffb3bcf2087fcd3c, so its change is decided by that squash's Port queue line. security: no
- `45d7b8bbd0c288e8d86aece2505db525486980aa` SUPERSEDED-BY-SQUASH: pre-squash commit of upstream PR #37 on branch fix/fresh-restore-retry (let the same --fresh command finish a failed recovery). The branch tip b22189884ed7 has exactly the tree of squash bba95f274216968542497a500c74a06802c48e88 on upstream main (`git diff b22189884ed7 bba95f274216` is empty), so its whole change is decided by that squash's Port queue line. security: no
- `b22189884ed779a8b61ec551c68840e33a209cbc` SUPERSEDED-BY-SQUASH: pre-squash commit of upstream PR #37 that skips the pre-restore safety copies on a continued --fresh (lib/pasarguard-restore.sh, docs, tests/unit_restore_recovery.sh); the branch tip's tree equals squash bba95f274216968542497a500c74a06802c48e88, so its change is decided by that squash's Port queue line. The branch's merge 46c79677d0e6 of upstream main is a clean merge with no change of its own. security: no

## Port queue

Re-checked on 2026-10-17 20:53 UTC: fork main 51edabbd0c62 now has upstream main bba95f274216 (#23 to #37) as an
ancestor, so the thirteen upstream main commits this queue held are ported. They are past the last release
v0.1.9; the 2026-10-14 decision 7 keeps those ports on scripts main. Any new dev-only upstream scripts commit is
held instead. No commit is owed.
