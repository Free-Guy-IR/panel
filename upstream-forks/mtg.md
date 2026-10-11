# Upstream ledger: mtg

Upstream commits of 9seconds/mtg that Free-Guy-IR/mtg does not carry as an ancestor, a patch-identical copy or a
cherry-pick or `Upstream:` trailer on its master branch, each with a decision. Checked by `scripts/upstream_forks.py`
from the panel's upstream-inventory workflow. Format and rules are the same as the panel's UPSTREAM_SYNC.md, including
`until: YYYY-MM-DD` deadlines. Reviewed on 2026-10-06 against upstream `master` 753d051259a9 (merge of PR #549 that
day), `stable` 95db895136b4, `v2` 83a31e04585a, `v1` 64859069ed82, the four pull request branches below and all 48
upstream tags. Fork `master` 7d0f004a01dd is upstream master d095108334de, the node's current upstream `require`, plus
28 fork commits. Upstream `stable`, `v2` and every tag from 0.9 to v2.2.8 add only merge commits past fork master, and
each of those merges equals its automatic merge result. The v1 commits 6f3abf09900d and b2de98c8f960 are
patch-identical to b53ead43720a and 14dfb9506a3d on fork master and need no line. The node links mtg only as the
library packages `mtglib`, `antireplay`, `ipblocklist` and `network/v2`; it never imports `internal/` packages and
never builds the mtg binary.

## Ported by design

- `b86a9cf85d2dfd26a523a2e93c9eaeb89c5b5fea` ALREADY-IN-FORK: Upstream branch `contrib/sni-router-host-mode` (PR #520, not merged into master) moves HAProxy in `contrib/sni-router` to host networking so the PROXY v2 header carries real client IPs, publishes mtg and Caddy on loopback only and drops the `ip_unprivileged_port_start` sysctl. Upstream took the same change through PR #522 instead: 4a4e00198032, b083d7573124 and a7febc2bf2be, merged by 2d7c71657cf7, all ancestors of fork master 7d0f004a01dd. Fork master `contrib/sni-router/docker-compose.yml` has `network_mode: host` for HAProxy, loopback-only published ports for mtg and Caddy, and no sysctl line. The one difference is the Caddy `proxy_protocol` allow list: #520 narrows it to loopback, while the merged form also allows the private ranges. This is an example deployment under `contrib/` that the node never uses.

## Waived

- `3e0880f6b4f520ad9c709ddfbdd4e7e66545db03` WAIVED: Upstream `v1` maintenance line (tag v1.0.10), the obsolete major of module `github.com/9seconds/mtg`, which split from master at 7718f62477ee in 2021-03. It fixes golangci-lint 1.42 findings in v1 packages (`config/`, `hub/`, `wrappers/`, `utils/`, `proxy/`). 28 of its 29 files do not exist on fork master, which builds only module `github.com/9seconds/mtg/v2`; the remaining file, `.github/workflows/ci.yaml`, has v2 content there.
- `c178b4b9ccb3d0d48a9e401c3cb2d0a6c612f5a0` WAIVED: `v1` line (tag v1.0.10): moves the v1 module, Dockerfile and CI to Go 1.17. Fork master is module `github.com/9seconds/mtg/v2` on `go 1.26`, so the v1 `go.mod`, `go.sum` and Dockerfile changes do not apply.
- `4b1f823777bfac6617fa46478c565e95fd816216` WAIVED: `v1` line (tag v1.0.10): dependency update of the v1 module's `go.mod` and `go.sum`. Fork master's `go.mod` is the separate v2 module with its own dependency set.
- `8da55410def30e70e27220d0901abbf795455367` WAIVED: `v1` line (tag v1.0.11): gofumpt formatting of v1 packages (`proxy/`, `stats/`, `telegram/`, `wrappers/`, `utils/`) and the v1 Makefile. None of its 12 files exist on fork master.
- `e74726af70841a81f2860352e8ae7a2bebdb67e5` WAIVED: `v1` line (tag v1.0.11): Go 1.18 updates of the v1 code (`mtproto/rpc/`, `wrappers/`, `telegram/`, `faketls/` and others). 25 of its 29 files do not exist on fork master; the other four (`go.mod`, `go.sum`, `.golangci.toml`, CI) hold v2 content there on `go 1.26`.
- `981ffb0dbe9267eb8ac14d9023c4d1765247435d` WAIVED: `v1` line (tag v1.0.11): changes the v1 Makefile's `update-deps` target. Fork master has no Makefile.
- `18a52340faecec84bba3bde597368878895ccb09` WAIVED: `v1` line (tag v1.0.11): dependency update of the v1 module's `go.mod` and `go.sum`, which fork master, the v2 module, does not use.
- `a587f85dd24a383b2f0f2bde66b11a42a61f4c1b` WAIVED: `v1` line (tag v1.0.11): a goreleaser setup for v1 binaries (`.goreleaser.yml`, `.gitignore`, Makefile). Fork master carries master's own v2 `.goreleaser.yml`, and the node never builds the mtg binary.
- `f48156814bbed345a1c6aaf2e033b87e9fda7c44` WAIVED: `v1` line (tag v1.0.12): Go 1.19 updates across the v1 code. 54 of its 60 files do not exist on fork master; the other six (`go.mod`, `go.sum`, `main.go`, Dockerfile, `.golangci.toml`, CI) hold v2 content there on `go 1.26`.
- `c1935b4cef259c0ead73b7b356d8c53ffe590e2b` WAIVED: `v1` line (tag v1.0.12): dependency update of the v1 module's `go.mod` and `go.sum`, which fork master, the v2 module, does not use.
- `64859069ed82d1ccf3f941b43698e5532cfdd9b8` WAIVED: `v1` head and tag v1.0.12: "Align buildinfo with master" copies master's build-info approach into v1 `buildinfo.go` and `main.go`. Fork master already has master's own `buildinfo.go`, and v1 is never built by the fork or the node.
- `b6ec95b9a7755aae00c8903195508004f6de9e68` UNMERGED-WIP: Only on upstream branch `docs/sni-router-version-note` (PR #512, not merged into master or any tag). It rewords three lines of `contrib/sni-router/README.md`, replacing "Requires mtg ≥ 2.4" with a pointer to #480. Upstream master 753d051259a9 and fork master still have the old wording at line 120. Docs for an example deployment the node never uses. until: 2026-11-05
- `f372866982a063c8abd12ae0af46385cc97bdf11` UNMERGED-WIP: Only on upstream branch `log-time-format` (open PR #568, not merged into master or any tag). It adds a `logTimeFormat` option in `internal/config` and `internal/cli/run_proxy.go` plus `example.config.toml`; 5 of its 261 added lines exist on upstream master. These are internal packages of the mtg binary, which the node cannot import. until: 2026-11-05
- `90551fb8f4a8aa0c1b4c29c7ceecb158a4c49683` UNMERGED-WIP: Only on upstream branch `sni-router-two-service-pointer` (open PR #571, not merged into master or any tag). It adds a README section to `contrib/sni-router` that points single-domain users at the wiki's two-service setup; none of its 11 added lines are on upstream master. until: 2026-11-05

## Port queue

- `d2722ef5c80241704572e9f2b45d2123f8b7838c` PORT-NEEDED: Upstream master took it on 2026-10-06 through merge 753d051259a9 (PR #549, "doctor: validate fronting domain TLS certificate"): the `mtg doctor` fronting-domain step now runs a verified TLS handshake, in `internal/cli/doctor.go` and its test. Fork master 7d0f004a01dd lacks it (6 of 163 added lines present). No port exists: the fork's `doctor-tls-cert-check` branch is only the mirrored upstream PR branch, not merged into fork master. `git merge-tree --write-tree` of fork master and upstream master 753d051259a9 merges with no conflict, and no fork commit touches `internal/cli`. It changes only the mtg CLI, so the node's library build is unaffected. Fork master must still contain upstream master before the node's `require` of mtg can move past d095108334de, because the node drift check refuses a `replace` commit that lacks the required upstream commit. security: no until: 2026-10-20
