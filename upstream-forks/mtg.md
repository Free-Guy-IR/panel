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

Re-checked on 2026-11-03 against upstream `master` 9a2e120618d0, `stable` fab35cd6aa49, `v2` 5899a2f5b3bd, which is
the new release tag v2.3.0 (published 2026-11-03, tree identical to master 9a2e120618d0), `v1` 64859069ed82, branches
`log-time-format` 1e2af2509888, `docs/sni-router-version-note` b6ec95b9a775 and `contrib/sni-router-host-mode`
b86a9cf85d2d, the mirror's `release/v2.3.0` fab35cd6aa49 (deleted upstream after the release) and all 49 upstream
tags (`git ls-remote` of 9seconds/mtg the same day). Branch `sni-router-two-service-pointer` is gone: PR #571 merged
it into master as be6e17602542 on 2026-10-22. Fork `master` is still 7d0f004a01dd, the commit the node `go.mod`
`replace` pins; fork `main` fecb51f40f35 is fork master plus one Go 1.26.10 build commit and is not the node's pin.
No fork commit ports any of the upstream commits past master 753d051259a9, and `git merge-tree --write-tree` of fork
master and upstream master 9a2e120618d0 merges with no conflict. Upstream v2.3.0 now contains every master commit
listed below. Two of them, e660dc1c22ed and 7931f8146886, fix ways an accept error ends `Proxy.Serve` for good,
and the node runs `_ = pi.proxy.Serve(listener)` in backend/mtproto/mtproto.go without a retry or restart, so an
EMFILE under a connection flood or a Linux accept errno of one new connection stops that MTProto inbound until the
core restarts. That is a remotely triggerable denial of service, so both are security ports. They were owed by the
MTG lane of 2026-11-03, which merged upstream v2.3.0 into fork main the same day (see Port queue); the ledger
now tracks fork main, the fork's default branch, instead of fork master. The
node's own middle-proxy path (`_ = pi.mp.Serve(listener)`, backend/mtproto/middleproxy/instance.go:64-75 on node
main 28db4435d7cf) stops for good on an accept error in the same way; that is node code, which the merge does not
fix, and it is owed by NODE-LEAD in the node ledger.

## Ported by design

- `b86a9cf85d2dfd26a523a2e93c9eaeb89c5b5fea` ALREADY-IN-FORK: Upstream branch `contrib/sni-router-host-mode` (PR #520, not merged into master) moves HAProxy in `contrib/sni-router` to host networking so the PROXY v2 header carries real client IPs, publishes mtg and Caddy on loopback only and drops the `ip_unprivileged_port_start` sysctl. Upstream took the same change through PR #522 instead: 4a4e00198032, b083d7573124 and a7febc2bf2be, merged by 2d7c71657cf7, all ancestors of fork master 7d0f004a01dd. Fork master `contrib/sni-router/docker-compose.yml` has `network_mode: host` for HAProxy, loopback-only published ports for mtg and Caddy, and no sysctl line. The one difference is the Caddy `proxy_protocol` allow list: #520 narrows it to loopback, while the merged form also allows the private ranges. This is an example deployment under `contrib/` that the node never uses.

- `00a27e407da4db5c73c4dc0dfde967d1c8d650ee` SUPERSEDED-BY-FORK: only on upstream branch `fix/shutdown-connection-race` (PR #588, open; head 00a27e407da4 on 2026-11-03, in no upstream tag and not on master or stable). In ServeConn it makes the context.AfterFunc that fires when the stream context is cancelled close only the accepted connection, because closing the whole stream context there raced ServeConn replacing ctx.clientConn and ctx.telegramConn during setup. The fork fixed the same race in its own form before upstream: fork commit 6e378787be50 closes a cancelled stream through its accepted connection and a locked telegram connection (mtglib/proxy.go, mtglib/stream_context.go), and e3e9542730f6 tests that the close does not race the handshake or the telegram dial; both are ancestors of fork master 7d0f004a01dd, the commit the node go.mod pins, and of fork main 7f8c04762e3b. The node tracker records it the same way (a branch: entry in node drift, superseded by 6e378787). security: no, the race needs the proxy context to be cancelled while a stream is still in setup, which Shutdown does; the stream's own end stops the AfterFunc first, so a client alone does not reach it

## Waived

- `3e0880f6b4f520ad9c709ddfbdd4e7e66545db03` WAIVED: Upstream `v1` maintenance line (tag v1.0.10), the obsolete major of module `github.com/9seconds/mtg`, which split from master at 7718f62477ee in 2021-03. It fixes golangci-lint 1.42 findings in v1 packages (`config/`, `hub/`, `wrappers/`, `utils/`, `proxy/`). 28 of its 29 files do not exist on fork master, which builds only module `github.com/9seconds/mtg/v2`; the remaining file, `.github/workflows/ci.yaml`, has v2 content there. security: no, lint fixes in a v1 module that neither the fork nor the node builds
- `c178b4b9ccb3d0d48a9e401c3cb2d0a6c612f5a0` WAIVED: `v1` line (tag v1.0.10): moves the v1 module, Dockerfile and CI to Go 1.17. Fork master is module `github.com/9seconds/mtg/v2` on `go 1.26`, so the v1 `go.mod`, `go.sum` and Dockerfile changes do not apply. security: no, a toolchain bump of the unbuilt v1 module
- `4b1f823777bfac6617fa46478c565e95fd816216` WAIVED: `v1` line (tag v1.0.10): dependency update of the v1 module's `go.mod` and `go.sum`. Fork master's `go.mod` is the separate v2 module with its own dependency set. security: no, dependency pins of the unbuilt v1 module
- `8da55410def30e70e27220d0901abbf795455367` WAIVED: `v1` line (tag v1.0.11): gofumpt formatting of v1 packages (`proxy/`, `stats/`, `telegram/`, `wrappers/`, `utils/`) and the v1 Makefile. None of its 12 files exist on fork master. security: no, formatting only
- `e74726af70841a81f2860352e8ae7a2bebdb67e5` WAIVED: `v1` line (tag v1.0.11): Go 1.18 updates of the v1 code (`mtproto/rpc/`, `wrappers/`, `telegram/`, `faketls/` and others). 25 of its 29 files do not exist on fork master; the other four (`go.mod`, `go.sum`, `.golangci.toml`, CI) hold v2 content there on `go 1.26`. security: no, a toolchain bump of the unbuilt v1 module
- `981ffb0dbe9267eb8ac14d9023c4d1765247435d` WAIVED: `v1` line (tag v1.0.11): changes the v1 Makefile's `update-deps` target. Fork master has no Makefile. security: no, a build helper target of the v1 line
- `18a52340faecec84bba3bde597368878895ccb09` WAIVED: `v1` line (tag v1.0.11): dependency update of the v1 module's `go.mod` and `go.sum`, which fork master, the v2 module, does not use. security: no, dependency pins of the unbuilt v1 module
- `a587f85dd24a383b2f0f2bde66b11a42a61f4c1b` WAIVED: `v1` line (tag v1.0.11): a goreleaser setup for v1 binaries (`.goreleaser.yml`, `.gitignore`, Makefile). Fork master carries master's own v2 `.goreleaser.yml`, and the node never builds the mtg binary. security: no, release packaging of v1 binaries
- `f48156814bbed345a1c6aaf2e033b87e9fda7c44` WAIVED: `v1` line (tag v1.0.12): Go 1.19 updates across the v1 code. 54 of its 60 files do not exist on fork master; the other six (`go.mod`, `go.sum`, `main.go`, Dockerfile, `.golangci.toml`, CI) hold v2 content there on `go 1.26`. security: no, a toolchain bump of the unbuilt v1 module
- `c1935b4cef259c0ead73b7b356d8c53ffe590e2b` WAIVED: `v1` line (tag v1.0.12): dependency update of the v1 module's `go.mod` and `go.sum`, which fork master, the v2 module, does not use. security: no, dependency pins of the unbuilt v1 module
- `64859069ed82d1ccf3f941b43698e5532cfdd9b8` WAIVED: `v1` head and tag v1.0.12: "Align buildinfo with master" copies master's build-info approach into v1 `buildinfo.go` and `main.go`. Fork master already has master's own `buildinfo.go`, and v1 is never built by the fork or the node. security: no, build metadata of the v1 binary
- `b6ec95b9a7755aae00c8903195508004f6de9e68` UNMERGED-WIP: Only on upstream branch `docs/sni-router-version-note` (PR #512), re-checked 2026-11-03: still not in upstream master 9a2e120618d0 or any tag, including v2.3.0. It rewords three lines of `contrib/sni-router/README.md`, replacing "Requires mtg ≥ 2.4" with a pointer to #480. Upstream master and fork master still have the old wording. Docs for an example deployment the node never uses. until: 2026-12-03 security: no, a README wording change
- `f372866982a063c8abd12ae0af46385cc97bdf11` UNMERGED-WIP: Only on upstream branch `log-time-format` (PR #568), re-checked 2026-11-03: the branch moved to 1e2af2509888 after merging master (b6559a876fbe), and this commit is still not in upstream master 9a2e120618d0 or any tag, including v2.3.0. It adds a `logTimeFormat` option in `internal/config` and `internal/cli/run_proxy.go` plus `example.config.toml`. These are internal packages of the mtg binary, which the node cannot import. until: 2026-12-03 security: no, a log timestamp format option
- `1e2af250988804fec01dde9284b5e06c0382f716` UNMERGED-WIP: Only on upstream branch `log-time-format` (PR #568), not in upstream master 9a2e120618d0 or any tag, including v2.3.0. A test change in `internal/cli/run_proxy_test.go` that restores the logger time format after the preset checks of f372866982a0. Internal CLI test of the mtg binary, which the node never builds. until: 2026-12-03 security: no, a test cleanup

## Port queue

Empty since 2026-11-03. The MTG lane merged upstream v2.3.0 into fork main as 06a89488146c (fork main 7f8c04762e3b by `git ls-remote` at 15:11 +0330), so the 14 commits listed here until then, among them the security pair e660dc1c22ed and 7931f8146886, are merged into fork main with every line present, which the gate now checks. Fork master and the node's go.mod `replace` still name 7d0f004a01dd, which has none of them: the node runs these fixes only once NODE-LEAD moves that pin to a fork main commit.
