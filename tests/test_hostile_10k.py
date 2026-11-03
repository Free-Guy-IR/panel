import importlib.util
from pathlib import Path

import pytest

from tests.test_hostile_10c import hostile_world
from tests.test_upstream_forks import run

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "upstream_inventory.py"
SIDE = "f0e1d2c3b4a5968778695a4b3c2d1e0f98765432"
GHOST = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
SELF = "0badc0ffee0badc0ffee0badc0ffee0badc0ffee"


def load_inventory():
    spec = importlib.util.spec_from_file_location("upstream_inventory_10k", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_10k_shipped_arches_are_node_release_yml_linux_amd64_386_arm_arm64_and_docker_amd64_arm64():
    assert load_inventory().GO_SHIPPED_ARCHES == frozenset({"amd64", "arm64", "386", "arm"})


@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("pkg/x_amd64.go", "package x\n"),
        ("pkg/x_linux_amd64.go", "package x\n"),
        ("pkg/x_arm64.go", "package x\n"),
        ("pkg/x_linux_arm64.go", "package x\n"),
        ("pkg/x_arm.go", "package x\n"),
        ("pkg/x_linux_arm.go", "package x\n"),
        ("pkg/x.go", "//go:build amd64\n\npackage x\n"),
        ("pkg/x.go", "//go:build arm64\n\npackage x\n"),
        ("pkg/x.go", "//go:build arm\n\npackage x\n"),
        ("pkg/x.go", "// +build amd64\n\npackage x\n"),
        ("pkg/x.go", "//go:build !386\n\npackage x\n"),
        ("pkg/x.go", "//go:build !arm\n\npackage x\n"),
        ("pkg/x.go", "//go:build !amd64\n\npackage x\n"),
        ("pkg/x.go", "//go:build !arm64\n\npackage x\n"),
    ],
    ids=[
        "suffix-amd64",
        "suffix-linux-amd64",
        "suffix-arm64",
        "suffix-linux-arm64",
        "suffix-arm",
        "suffix-linux-arm",
        "tag-amd64",
        "tag-arm64",
        "tag-arm",
        "legacy-tag-amd64",
        "tag-not-386",
        "tag-not-arm",
        "tag-not-amd64",
        "tag-not-arm64",
    ],
)
def test_10k_file_missing_from_any_shipped_linux_target_is_excluded(path, content):
    assert load_inventory().go_build_excluded(path, content, 22)


@pytest.mark.parametrize(
    "content",
    [
        "//go:build !s390x\n\npackage x\n",
        "//go:build !riscv64\n\npackage x\n",
        "//go:build !mips64le\n\npackage x\n",
        "//go:build linux\n\npackage x\n",
        "package x\n",
    ],
    ids=["not-s390x", "not-riscv64", "not-mips64le", "linux", "plain"],
)
def test_10k_file_built_on_every_shipped_linux_target_is_kept(content):
    assert not load_inventory().go_build_excluded("pkg/x.go", content, 22)


def moved_world(tmp_path, target, header):
    base_files = {"a.go": "one\nTOKEN_OK\ntwo\n", "b.go": "func handler() {\n\tthree\n\tfour\n}\n"}
    fix_files = {
        "a.go": "one\nTOKEN_OK\nCHECK_TOKEN\ntwo\n",
        "b.go": "func handler() {\n\tthree\n\tCHECK_SCOPE\n\tfour\n}\n",
    }
    steps = [
        ("token", {"a.go": "one\nTOKEN_OK\nCHECK_TOKEN\ntwo\n"}),
        ("move", {target: f"{header}package live\n\nfunc handler() {{\n\tthree\n\tCHECK_SCOPE\n\tfour\n}}\n"}),
    ]
    config, _ = hostile_world(
        tmp_path, base_files, fix_files, steps, ["token"], f" missing `b.go:+3` because <move> moved it to `{target}`;"
    )
    return run(config, "--today", "2026-10-27")


@pytest.mark.parametrize(
    ("target", "header"),
    [
        ("c_amd64.go", ""),
        ("c_linux_amd64.go", ""),
        ("c.go", "//go:build !386\n\n"),
        ("c.go", "//go:build arm64\n\n"),
    ],
    ids=["amd64-only-file", "linux-amd64-only-file", "not-on-386", "arm64-only-tag"],
)
def test_10k_moved_into_a_file_absent_from_a_shipped_target_is_refused(tmp_path, target, header):
    result = moved_world(tmp_path, target, header)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "excluded from the build" in result.stdout, result.stdout


@pytest.fixture
def inventory(monkeypatch):
    module = load_inventory()
    known = {SIDE: SIDE}
    monkeypatch.setattr(
        module, "resolve_commit", lambda token: next((sha for key, sha in known.items() if key.startswith(token)), None)
    )
    monkeypatch.setattr(module, "is_ancestor", lambda ancestor, descendant: False)
    monkeypatch.setattr(module, "on_upstream_ref", lambda sha: False)
    return module


@pytest.mark.parametrize(
    "reason",
    [
        f"ported in {GHOST[:12]} security: no",
        f"carried by {GHOST[:12]} security: no",
        f"fixed by {GHOST} security: no",
        f"Ported In {SIDE[:12]} and {GHOST[:12]} security: no",
        f"fixed by {SIDE[:12]}, {GHOST[:12]} security: no",
    ],
    ids=["ported-in-ghost", "carried-by-ghost", "fixed-by-ghost-full", "ported-in-list-ghost", "fixed-by-comma-ghost"],
)
def test_10k_ghost_cited_with_port_wording_is_flagged(inventory, reason):
    problems = inventory.cited_history_problems(
        {"where": "kit.md:3", "token": SELF, "reason": reason}, "main", frozenset({SELF})
    )
    assert any(GHOST[:12] in problem for problem in problems), problems


@pytest.mark.parametrize(
    "reason",
    [
        "carried by the alembic revision 1a2b3c4d5e6f security: no",
        "fixed by raising the limit to 1000000000 bytes security: no",
        "ported in go.mod pin v0.0.0-20260101120000-abcdef123456 security: no",
    ],
    ids=["carried-by-alembic", "fixed-by-number", "ported-in-pseudo-version"],
)
def test_10k_port_wording_not_followed_by_a_hash_is_ignored(inventory, reason):
    problems = inventory.cited_history_problems(
        {"where": "kit.md:3", "token": SELF, "reason": reason}, "main", frozenset({SELF})
    )
    assert problems == [], problems
