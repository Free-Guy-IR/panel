import importlib.util
import subprocess
from pathlib import Path

import pytest

from tests.test_hostile_10c import hostile_world
from tests.test_upstream_forks import run

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "upstream_inventory.py"
NODE_CLONE = next(
    (folder / "node" for folder in Path(__file__).resolve().parents if (folder / "node" / ".git").exists()),
    Path("node"),
)
SIDE = "f0e1d2c3b4a5968778695a4b3c2d1e0f98765432"
GHOST = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
SELF = "0badc0ffee0badc0ffee0badc0ffee0badc0ffee"

RELEASE_MATRIX = """name: Create Release
on:
  release:
    types: [published]
jobs:
  build:
    strategy:
      matrix:
        goos:
          # - windows
          - openbsd
          - linux
        goarch: [amd64, 386]
        include:
          # - goos: linux
          #   goarch: riscv64
          - goos: linux
            goarch: arm
            goarm: 7
          - goos: linux
            goarch: arm
            goarm: 5
          - goos: linux
            goarch: arm64
          - goos: linux
            goarch: s390x # BEGIN S390X
          - goos: openbsd
            goarch: arm64
    env:
      CGO_ENABLED: 0
"""


def load_inventory():
    spec = importlib.util.spec_from_file_location("upstream_inventory_10m", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "content",
    ["//go:build !s390x\n\npackage x\n", "// +build !s390x\n\npackage x\n"],
    ids=["go-build", "legacy"],
)
def test_10m_file_not_built_on_shipped_linux_s390x_is_excluded(content):
    assert load_inventory().go_build_excluded("pkg/x.go", content, 26)


@pytest.mark.parametrize(
    "content",
    [
        'package x\n\nimport "C"\n',
        'package x\n\nimport (\n\t"fmt"\n\t"C"\n)\n',
        "package x\n\nimport `C`\n",
    ],
    ids=["single", "grouped", "raw-string"],
)
def test_10m_cgo_file_is_excluded_because_every_shipped_build_sets_cgo_enabled_0(content):
    assert load_inventory().go_build_excluded("pkg/x.go", content, 26)


def test_10m_plain_string_c_outside_an_import_is_still_built():
    assert not load_inventory().go_build_excluded("pkg/x.go", 'package x\n\nimport "fmt"\n\nvar grade = "C"\n', 26)


def test_10m_bom_prefixed_constraint_is_honoured_like_go():
    assert load_inventory().go_build_excluded("pkg/x.go", "\ufeff//go:build ignore\n\npackage x\n", 26)


def test_10m_linux_only_file_is_kept_because_openbsd_assets_are_outside_the_linux_production_scope():
    assert not load_inventory().go_build_excluded("pkg/x.go", "//go:build linux\n\npackage x\n", 26)


def test_10m_matrix_parser_reads_linux_goarch_with_include_and_ignores_comments():
    assert load_inventory().workflow_linux_arches(RELEASE_MATRIX) == frozenset(
        {"amd64", "386", "arm", "arm64", "s390x"}
    )


def test_10m_matrix_parser_applies_exclude_on_goos_and_goarch():
    text = RELEASE_MATRIX.replace(
        "        include:\n", "        exclude:\n          - goos: linux\n            goarch: 386\n        include:\n"
    )
    assert load_inventory().workflow_linux_arches(text) == frozenset({"amd64", "arm", "arm64", "s390x"})


def test_10m_matrix_parser_keeps_an_exclude_that_names_a_key_it_cannot_judge():
    text = RELEASE_MATRIX.replace(
        "        include:\n",
        '        exclude:\n          - goos: linux\n            gotoolchain: ""\n        include:\n',
    )
    assert load_inventory().workflow_linux_arches(text) == frozenset({"amd64", "386", "arm", "arm64", "s390x"})


def test_10m_matrix_parser_reads_docker_platforms():
    text = "jobs:\n  b:\n    steps:\n      - uses: docker/build-push-action@v7\n        with:\n          platforms: linux/amd64,linux/arm64, linux/arm/v7\n"
    assert load_inventory().workflow_linux_arches(text) == frozenset({"amd64", "arm64", "arm"})


@pytest.mark.parametrize(
    "text",
    [
        "jobs:\n  b:\n    strategy:\n      matrix:\n        goos: ${{ fromJSON(needs.a.outputs.os) }}\n        goarch: [amd64]\n",
        "jobs:\n  b:\n    strategy:\n      matrix: ${{ fromJSON(needs.a.outputs.m) }}\n    env:\n      GOOS: ${{ matrix.goos }}\n",
        "jobs:\n  b:\n    strategy:\n      matrix:\n        goos: [linux]\n        include:\n          - goarch: riscv64\n",
        "jobs:\n  b:\n    steps:\n      - with:\n          platforms: ${{ env.PLATFORMS }}\n",
    ],
    ids=["expression-goos", "expression-matrix", "include-without-goos", "expression-platforms"],
)
def test_10m_matrix_parser_fails_closed_on_what_it_cannot_read(text):
    module = load_inventory()
    with pytest.raises(module.ShippedArchesUnreadable):
        module.workflow_linux_arches(text)


def node_workflow_arches(ref):
    module = load_inventory()
    names = subprocess.run(
        ["git", "-C", str(NODE_CLONE), "ls-tree", "--name-only", f"{ref}:.github/workflows"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    found = set()
    for name in names:
        text = subprocess.run(
            ["git", "-C", str(NODE_CLONE), "show", f"{ref}:.github/workflows/{name}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        found |= module.workflow_linux_arches(text)
    return found


@pytest.mark.parametrize("ref", ["origin/main", "main"])
def test_10m_constant_equals_the_node_fork_linux_release_matrix_at_the_tracked_ref(ref):
    if not (NODE_CLONE / ".git").exists():
        pytest.skip(f"node clone not present at {NODE_CLONE}")
    assert node_workflow_arches(ref) == set(load_inventory().GO_SHIPPED_ARCHES)


def moved_world(tmp_path, target, header, extra=None):
    base_files = {"a.go": "one\nTOKEN_OK\ntwo\n", "b.go": "func handler() {\n\tthree\n\tfour\n}\n"}
    fix_files = {
        "a.go": "one\nTOKEN_OK\nCHECK_TOKEN\ntwo\n",
        "b.go": "func handler() {\n\tthree\n\tCHECK_SCOPE\n\tfour\n}\n",
    }
    move = {target: f"{header}package live\n\nfunc handler() {{\n\tthree\n\tCHECK_SCOPE\n\tfour\n}}\n"}
    move.update(extra or {})
    steps = [("token", {"a.go": "one\nTOKEN_OK\nCHECK_TOKEN\ntwo\n"}), ("move", move)]
    config, _ = hostile_world(
        tmp_path, base_files, fix_files, steps, ["token"], f" missing `b.go:+3` because <move> moved it to `{target}`;"
    )
    return run(config, "--today", "2026-10-27")


@pytest.mark.parametrize(
    "header",
    [
        "//go:build !s390x\n\n",
        "// +build !s390x\n\n",
        '/*\n#include <stdlib.h>\n*/\nimport "C"\n\n',
        "\ufeff//go:build ignore\n\n",
    ],
    ids=["not-s390x", "legacy-not-s390x", "cgo", "bom-ignore"],
)
def test_10m_moved_into_a_file_a_shipped_linux_build_skips_is_refused(tmp_path, header):
    result = moved_world(tmp_path, "c.go", header)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "excluded from the build" in result.stdout, result.stdout


def test_10m_arch_added_to_the_head_release_matrix_is_honoured_at_gate_time(tmp_path):
    workflow = RELEASE_MATRIX.replace(
        "          # - goos: linux\n          #   goarch: riscv64\n",
        "          - goos: linux\n            goarch: riscv64\n",
    )
    result = moved_world(tmp_path, "c.go", "//go:build !riscv64\n\n", {".github/workflows/release.yml": workflow})
    assert result.returncode == 1, result.stdout + result.stderr
    assert "excluded from the build" in result.stdout, result.stdout


def test_10m_commented_out_head_matrix_entry_does_not_widen_the_set(tmp_path):
    result = moved_world(tmp_path, "c.go", "//go:build !riscv64\n\n", {".github/workflows/release.yml": RELEASE_MATRIX})
    assert result.returncode == 0, result.stdout + result.stderr


def test_10m_unreadable_head_matrix_fails_closed(tmp_path):
    workflow = "jobs:\n  b:\n    strategy:\n      matrix:\n        goos: ${{ fromJSON(needs.a.outputs.os) }}\n        goarch: [amd64]\n"
    result = moved_world(tmp_path, "c.go", "", {".github/workflows/release.yml": workflow})
    assert result.returncode == 1, result.stdout + result.stderr
    assert "shipped linux arch set" in result.stdout, result.stdout


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
        f"carried by `{GHOST[:12]}` security: no",
        f"commit `{GHOST[:12]}` security: no",
        f"carried by: {GHOST[:12]} security: no",
        f"fixed by `{SIDE[:12]}`, `{GHOST[:12]}` security: no",
    ],
    ids=["carried-by-backtick", "commit-backtick", "carried-by-colon", "fixed-by-backtick-list"],
)
def test_10m_ghost_cited_with_backticks_or_colon_is_flagged(inventory, reason):
    problems = inventory.cited_history_problems(
        {"where": "kit.md:3", "token": SELF, "reason": reason}, "main", frozenset({SELF})
    )
    assert any(GHOST[:12] in problem for problem in problems), problems


def test_10m_compact_include_list_is_read():
    text = "jobs:\n  b:\n    strategy:\n      matrix:\n        goos: [linux]\n        goarch: [amd64]\n        include:\n        - goos: linux\n          goarch: riscv64\n"
    assert load_inventory().workflow_linux_arches(text) == frozenset({"amd64", "riscv64"})


@pytest.mark.parametrize(
    "text",
    [
        "jobs:\n  b:\n    strategy: {matrix: {goos: [linux], goarch: [riscv64]}}\n",
        'jobs:\n  b:\n    strategy:\n      "matrix":\n        goos: [linux]\n        goarch: [riscv64]\n',
    ],
    ids=["flow-strategy", "quoted-matrix-key"],
)
def test_10m_matrix_written_in_a_form_the_parser_does_not_read_fails_closed(text):
    module = load_inventory()
    with pytest.raises(module.ShippedArchesUnreadable):
        module.workflow_linux_arches(text)


@pytest.mark.parametrize(
    "content",
    [
        'package x\n\nimport (\n\t"fmt" // )\n\t"C"\n)\n',
        'package x\n\nimport /* c */ "C"\n',
    ],
    ids=["paren-in-comment", "block-comment"],
)
def test_10m_cgo_import_hidden_behind_a_comment_is_excluded(content):
    assert load_inventory().go_build_excluded("pkg/x.go", content, 26)


@pytest.mark.parametrize(
    "text",
    [
        "jobs:\n  b:\n    strategy:\n      matrix:\n        os: [linux]\n        arch: [riscv64]\n    env:\n      GOOS: ${{ matrix.os }}\n      GOARCH: ${{ matrix.arch }}\n",
        "jobs:\n  b:\n    steps:\n      - run: for a in amd64 riscv64; do GOOS=linux GOARCH=$a go build; done\n",
        'jobs:\n  b:\n    steps:\n      - run: |\n          export GOARCH="${ARCH}"\n          go build\n',
        "jobs:\n  b:\n    steps:\n      - run: docker buildx build --platform linux/${{ matrix.arch }} .\n",
        'jobs:\n  b:\n    steps:\n      - run: docker buildx build --platform "$PLATFORMS" .\n',
        "jobs:\n  b:\n    steps:\n      - uses: wangyoucao577/go-release-action@v1\n        with:\n          goos: linux\n          goarch: ${{ inputs.arch }}\n",
        "jobs:\n  b:\n    env:\n      GOARCH:\n    steps:\n      - run: go build\n",
    ],
    ids=[
        "other-matrix-key",
        "shell-loop",
        "export-shell-var",
        "platform-flag-expression",
        "platform-flag-shell-var",
        "action-input-expression",
        "empty-goarch",
    ],
)
def test_10m_arch_set_from_a_source_the_parser_does_not_read_fails_closed(text):
    module = load_inventory()
    with pytest.raises(module.ShippedArchesUnreadable):
        module.workflow_linux_arches(text)


@pytest.mark.parametrize(
    ("text", "arches"),
    [
        ("jobs:\n  b:\n    steps:\n      - run: GOOS=linux GOARCH=riscv64 make build\n", {"riscv64"}),
        (
            "jobs:\n  b:\n    steps:\n      - run: |\n          export GOARCH='riscv64'\n          go build\n",
            {"riscv64"},
        ),
        ("jobs:\n  b:\n    steps:\n      - run: docker buildx build --platform linux/riscv64 .\n", {"riscv64"}),
        (
            "jobs:\n  b:\n    steps:\n      - run: docker build --platform=linux/amd64,linux/riscv64 .\n",
            {"amd64", "riscv64"},
        ),
        ('jobs:\n  b:\n    env:\n      GOOS: linux\n      GOARCH: "riscv64"\n', {"riscv64"}),
        ("jobs:\n  b:\n    steps:\n      - with:\n          goos: linux\n          goarch: riscv64\n", {"riscv64"}),
        (
            'jobs:\n  b:\n    strategy:\n      matrix:\n        goos: [linux]\n        goarch: [amd64]\n    env:\n      GOARCH: ${{ matrix.goarch }}\n    steps:\n      - run: echo "${GOOS}-${GOARCH}" ${{ env.GOARCH }}\n',
            {"amd64"},
        ),
    ],
    ids=[
        "run-env",
        "export-quoted",
        "platform-flag",
        "platform-flag-equals",
        "env-literal",
        "action-input-literal",
        "matrix-goarch-reference",
    ],
)
def test_10m_literal_goarch_and_platform_flag_are_read(text, arches):
    assert load_inventory().workflow_linux_arches(text) == frozenset(arches)


@pytest.mark.parametrize(
    "workflow",
    [
        "jobs:\n  b:\n    steps:\n      - run: GOOS=linux GOARCH=riscv64 make build\n",
        "jobs:\n  b:\n    steps:\n      - run: docker buildx build --platform linux/riscv64 .\n",
    ],
    ids=["run-env", "platform-flag"],
)
def test_10m_arch_set_on_a_run_line_at_head_is_honoured_at_gate_time(tmp_path, workflow):
    result = moved_world(tmp_path, "c.go", "//go:build !riscv64\n\n", {".github/workflows/release.yml": workflow})
    assert result.returncode == 1, result.stdout + result.stderr
    assert "excluded from the build" in result.stdout, result.stdout


def test_10m_goarch_fed_by_another_matrix_key_at_head_fails_closed(tmp_path):
    workflow = "jobs:\n  b:\n    strategy:\n      matrix:\n        os: [linux]\n        arch: [riscv64]\n    env:\n      GOARCH: ${{ matrix.arch }}\n"
    result = moved_world(tmp_path, "c.go", "", {".github/workflows/release.yml": workflow})
    assert result.returncode == 1, result.stdout + result.stderr
    assert "shipped linux arch set" in result.stdout, result.stdout


@pytest.mark.parametrize("ref", ["86da26e5", "2d8e11f3"])
def test_10m_real_node_workflows_stay_readable_at_both_heads(ref):
    if not (NODE_CLONE / ".git").exists():
        pytest.skip(f"node clone not present at {NODE_CLONE}")
    assert node_workflow_arches(ref) == set(load_inventory().GO_SHIPPED_ARCHES)


@pytest.mark.parametrize(
    "reason",
    [
        f"carried-by {GHOST[:12]} security: no",
        f"carried by {SIDE[:12]} and also {GHOST[:12]} security: no",
        f"fixed by ({GHOST[:12]}) security: no",
    ],
    ids=["hyphen", "and-also", "paren"],
)
def test_10m_ghost_cited_with_hyphen_and_also_or_paren_is_flagged(inventory, reason):
    problems = inventory.cited_history_problems(
        {"where": "kit.md:3", "token": SELF, "reason": reason}, "main", frozenset({SELF})
    )
    assert any(GHOST[:12] in problem for problem in problems), problems
