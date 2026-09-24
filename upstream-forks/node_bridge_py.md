# Upstream ledger: node_bridge_py

Upstream commits of PasarGuard/node_bridge_py that Free-Guy-IR/node_bridge_py does not carry as an ancestor, a patch-identical copy
or a cherry-pick or `Upstream:` trailer on its main branch, each with a decision. Checked by
`scripts/upstream_forks.py` from the panel's upstream-inventory workflow. Format and rules are the
same as the panel's UPSTREAM_SYNC.md, including `until: YYYY-MM-DD` deadlines.

## Ported by design

- `3f4ca0a52feeb3f450a2abc425328f9b03e932d0` ALREADY-IN-FORK: The same patch (patch-id 9318ceaaf16d) is fork/shared-storage commit 560dc50e288fc13fa0f5bd082992cf403523a47b. Its content also reached fork main through b4b2af072f56d5529a609427d7206fad0a108924, which is patch-identical to upstream main's squash db14bde (#17). The tree of upstream shared-storage tip c82f1d5 is identical to db14bde (`git diff c82f1d5 db14bde` is empty).
- `c82f1d5aa3e04c47ef9bb70cae4fc43c2dbc3f4f` ALREADY-IN-FORK: The same patch (patch-id 6a44a5209059) is fork/shared-storage head e3ac749ae0755d75b4d604d00eb60a80920dc7eb. Its content is on fork main through b4b2af072f56d5529a609427d7206fad0a108924 (= upstream db14bde), whose tree equals this commit's tree.

## Waived

- `2b37c9538b058b792792228d31142491edd64527` WAIVED: This is a version-only bump to 0.9.0 (`__init__.py`, `pyproject.toml`, `uv.lock`). Every code commit of upstream 0.9.0 is patch-identical on fork main: 08be6e0=a4215d0, d94f922=f4489a3, fdb963a=8731203 and db14bde=b4b2af0. The fork uses its own local-version label (`0.9.0+singbox` in 16322e4b18f85a3c073a0e746ec2a91e651b89b4, now `0.9.1+singbox` at fork main `pyproject.toml:3`), so the upstream version string is not taken.
- `5223c2994e9dff6b13d820f4b26d64d843733158` WAIVED: This is a version-only bump to 0.9.1. The fork carries its own label `0.9.1+singbox` (fork main `pyproject.toml:3`). The code part of 0.9.1 is 22f1ef7, which is tracked separately above as PORT-NEEDED, so this line waives only the version string.

## Pending port, not counted until it lands

The gate ignores this section, so it reports these commits as unported until they are on the fork's
main branch. The port exists with `git cherry-pick -x` trailers on the unpublished bridge branch
update/2026-09-23 (head c67cb96), which is the bridge 0.9.2 work of owner P5; once it is merged into
Free-Guy-IR/node_bridge_py main, the trailers cover all three and these lines are deleted.

- `22f1ef7914e1039723b7f5fece58c57dc2b2f23c` PORT-NEEDED: The published fork does not have upstream 0.9.1's only code change: ruff lint fixes (the `exclude_inbounds=[]` mutable default becomes `None`, silent `except: pass` becomes debug logs, `logger.exception`, and the `[tool.ruff.lint]` ignores). `git apply --check` of this patch succeeds cleanly on the fork/main tree, which proves it is absent. Fork main and fork tag v0.9.1 (ffabffd) still say `version = "0.9.1+singbox"`, so the label claims a version whose code the fork lacks. A patch-identical port, 657be4d4a4709b798dbd0848f699596762521782 (same patch-id 81a9b686f454, with the cherry-pick trailer), exists only on the local, unpushed branch `update/2026-09-23` in `~/Projects/pasarguard/bridge`.
- `27be20638dc02a95cab02498bbdb35457481a71f` PORT-NEEDED: This is upstream PR #21 "make queued user synchronization reliable and bounded", a squash commit with a single parent: generation fences on full sync, the revisioned in-memory store, wake on connect, queued work that survives a disconnect, bounded claims and the user-sync resolver, across `controller.py`, `storage.py`, `__init__.py` and new tests. `git apply --check` fails in both directions on the fork/main tree, and the new files `tests/test_user_sync.py` and `tests/test_sync_transport.py` do not exist there. The panel therefore runs fork bridge ffabffd without the fix. A patch-identical port, 14890f8f4a7452bdbb1b5c8f234febf240611ece (same patch-id 46bd635e0d31, with a `(cherry picked from commit 27be206…)` trailer), exists only on the unpushed local branch `update/2026-09-23`; no fork remote branch contains it (`git branch -r --contains 14890f8` is empty).
- `bec9cb224992b6d249cd57fb953c4c147a297ccc` PORT-NEEDED: This is the version-only bump to 0.9.2. It must land together with 27be206, because the fork label has to move to `0.9.2+singbox` only once that code is present. Today fork main still says `0.9.1+singbox`, and `__init__.py:18` says a stale `__version__ = "0.8.1"`. The local port fa979789208dcb5624c4751b2224eed83e6a58d6 (with the cherry-pick trailer) sets `__version__ = "0.9.2+singbox"`, and e90165bd40f82d8394895bb993b882d45042f377 sets pyproject to `0.9.2+singbox`. Both are only on the unpushed `update/2026-09-23`.
