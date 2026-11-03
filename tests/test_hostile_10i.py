import importlib.util
from pathlib import Path

import pytest

from tests.test_hostile_10c import hostile_world
from tests.test_upstream_forks import NOT_CARRIED, comment_world, run

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "upstream_inventory.py"
SIDE = "f0e1d2c3b4a5968778695a4b3c2d1e0f98765432"
GHOST = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
SELF = "0badc0ffee0badc0ffee0badc0ffee0badc0ffee"
TAG_OBJECT = "7a6b5c4d3e2f1a0b9c8d7e6f5a4b3c2d1e0f9a8b"


def load_inventory():
    spec = importlib.util.spec_from_file_location("upstream_inventory_10i", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("upstream_text", "fork_text"),
    [
        (
            'one\n// int check_token(void) { return deny(); }\nimport /* c */ "C"\ntwo\n',
            'one\nimport /* c */ "C"\ntwo\n',
        ),
        (
            'one\n// int check_token(void) { return deny(); }\nimport cgo /* c */ "C"\ntwo\n',
            'one\nimport cgo /* c */ "C"\ntwo\n',
        ),
        (
            'one\nimport (\n\t// int check_token(void) { return deny(); }\n\t/* c */ "C"\n)\ntwo\n',
            'one\nimport (\n\t/* c */ "C"\n)\ntwo\n',
        ),
    ],
    ids=["cgo-block-comment-before-path", "cgo-alias-and-comment", "cgo-grouped-comment-before-path"],
)
def test_10i_cgo_preamble_behind_a_comment_is_not_relaxed(tmp_path, upstream_text, fork_text):
    config, fix = comment_world(tmp_path, upstream_text, fork_text, "a.go")
    result = run(config, "--today", "2026-10-27")
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"`{fix[:12]}` {NOT_CARRIED}" in result.stdout, result.stdout


@pytest.mark.parametrize(
    ("path", "content", "go_minor"),
    [
        ("pkg/x_test.go", "package x\n", 22),
        ("pkg/x_windows.go", "package x\n", 22),
        ("pkg/x_darwin_arm64.go", "package x\n", 22),
        ("pkg/x_linux_386.go", "package x\n", 22),
        ("pkg/x_386.go", "package x\n", 22),
        ("pkg/x_windows_amd64.go", "package x\n", 22),
        ("pkg/x.go", "//go:build windows\n\npackage x\n", 22),
        ("pkg/x.go", "//go:build linux && !linux\n\npackage x\n", 22),
        ("pkg/x.go", "//go:build linux || windows\n\npackage x\n", 22),
        ("pkg/x.go", "//go:build !linux\n\npackage x\n", 22),
        ("pkg/x.go", "//go:build cgo\n\npackage x\n", 22),
        ("pkg/x.go", "//go:build ignore\n\npackage x\n", 22),
        ("pkg/x.go", "//go:build go1.99\n\npackage x\n", 22),
        ("pkg/x.go", "//go:build go1.21\n\npackage x\n", None),
        ("pkg/x.go", "// +build windows\n\npackage x\n", 22),
    ],
    ids=[
        "g1-test-file",
        "g2-goos-suffix",
        "g2-goos-goarch-suffix",
        "g2-linux-unshipped-arch",
        "g2-unshipped-arch",
        "g2-foreign-os-shipped-arch",
        "g3-foreign-tag",
        "g3-unsatisfiable",
        "g3-disjunction",
        "g3-negated-linux",
        "g3-cgo-unknown",
        "g3-ignore",
        "g4-future-go",
        "g4-no-module-version",
        "g3-legacy-plus-build",
    ],
)
def test_10i_unbuilt_go_targets_are_excluded(path, content, go_minor):
    assert load_inventory().go_build_excluded(path, content, go_minor)


@pytest.mark.parametrize(
    ("path", "content", "go_minor"),
    [
        ("pkg/x.go", "package x\n", 22),
        ("pkg/x_linux.go", "package x\n", 22),
        ("pkg/x_linux_amd64.go", "package x\n", 22),
        ("pkg/x_arm64.go", "package x\n", 22),
        ("pkg/x.go", "//go:build linux\n\npackage x\n", 22),
        ("pkg/x.go", "//go:build !windows\n\npackage x\n", 22),
        ("pkg/x.go", "//go:build unix\n\npackage x\n", 22),
        ("pkg/x.go", "//go:build go1.21\n\npackage x\n", 22),
    ],
    ids=["plain", "linux-suffix", "linux-amd64-suffix", "arm64-suffix", "linux-tag", "not-windows", "unix", "old-go"],
)
def test_10i_linux_built_go_targets_are_kept(path, content, go_minor):
    assert not load_inventory().go_build_excluded(path, content, go_minor)


def test_10i_moved_block_into_a_test_file_is_refused(tmp_path):
    base_files = {"a.go": "one\nTOKEN_OK\ntwo\n", "b.go": "func handler() {\n\tthree\n\tfour\n}\n"}
    fix_files = {
        "a.go": "one\nTOKEN_OK\nCHECK_TOKEN\ntwo\n",
        "b.go": "func handler() {\n\tthree\n\tCHECK_SCOPE\n\tfour\n}\n",
    }
    steps = [
        ("token", {"a.go": "one\nTOKEN_OK\nCHECK_TOKEN\ntwo\n"}),
        ("move", {"c_test.go": "package dead\n\nfunc handler() {\n\tthree\n\tCHECK_SCOPE\n\tfour\n}\n"}),
    ]
    config, _ = hostile_world(
        tmp_path, base_files, fix_files, steps, ["token"], " missing `b.go:+3` because <move> moved it to `c_test.go`;"
    )
    result = run(config, "--today", "2026-10-27")
    assert result.returncode == 1, result.stdout + result.stderr


@pytest.fixture
def inventory(monkeypatch):
    module = load_inventory()
    known = {SIDE: SIDE, TAG_OBJECT: SIDE}
    monkeypatch.setattr(
        module, "resolve_commit", lambda token: next((sha for key, sha in known.items() if key.startswith(token)), None)
    )
    monkeypatch.setattr(module, "is_ancestor", lambda ancestor, descendant: False)
    monkeypatch.setattr(module, "on_upstream_ref", lambda sha: False)
    return module


@pytest.mark.parametrize(
    "reason",
    [
        "kept the alembic revision 1a2b3c4d5e6f as the down_revision security: no",
        "the default limit stays 1000000000 bytes security: no",
        "patch-id 3f9e8d7c6b5a49382716051f2e3d4c5b6a798081 matches the upstream patch security: no",
        "go.mod pins v0.0.0-20260101120000-abcdef123456 security: no",
        f"the release tag {TAG_OBJECT[:12]} peels to the release security: no",
    ],
    ids=["alembic-id", "number", "patch-id", "pseudo-version", "tag-object"],
)
def test_10i_unresolvable_hex_without_commit_wording_is_ignored(inventory, reason):
    problems = inventory.cited_history_problems(
        {"where": "kit.md:3", "token": SELF, "reason": reason}, "main", frozenset({SELF})
    )
    assert problems == [], problems


@pytest.mark.parametrize(
    "reason",
    [
        f"ported in fork commit {GHOST[:12]} security: no",
        f"ported in fork commits {SIDE[:12]} and {GHOST[:12]} security: no",
        f"carried by commit {GHOST} security: no",
        f"ported in {SIDE[:12]} on the fork security: no",
    ],
    ids=["ghost-fork-commit", "ghost-in-carrier-list", "ghost-commit-full", "resolving-side-commit"],
)
def test_10i_ghost_or_unmerged_cited_commit_is_still_flagged(inventory, reason):
    problems = inventory.cited_history_problems(
        {"where": "kit.md:3", "token": SELF, "reason": reason}, "main", frozenset({SELF})
    )
    assert problems, reason
