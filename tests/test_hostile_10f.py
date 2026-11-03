import importlib.util
from pathlib import Path

import pytest

from tests.test_hostile_10c import hostile_world
from tests.test_upstream_forks import NOT_CARRIED, comment_world, commit, git, new_repo, run

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "upstream_inventory.py"
SIDE = "f0e1d2c3b4a5968778695a4b3c2d1e0f98765432"
GHOST = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
SELF = "0badc0ffee0badc0ffee0badc0ffee0badc0ffee"


@pytest.mark.parametrize(
    ("name", "upstream_text", "fork_text"),
    [
        ("a.go", "one\nx := 1 /* start\nend */ CHECK_TOKEN()\ntwo\n", "one\nx := 1 /* start\nend */\ntwo\n"),
        ("a.go", "one\n/* a */ CHECK_TOKEN() /* b\n*/\ntwo\n", "one\n/* a */ /* b\n*/\ntwo\n"),
        ("a.py", "one\nCHECK_TOKEN()  # why\ntwo\n", "one\nOTHER()\ntwo\n"),
        (
            "a.js",
            "one\nconst re = /a/;\nCHECK_TOKEN();\nconst end = /x/;\ntwo\n",
            "one\nconst re = /a/;\nconst end = /x/;\ntwo\n",
        ),
        ("a.go", 'one\n// int check_token(void) { return deny(); }\nimport "C"\ntwo\n', 'one\nimport "C"\ntwo\n'),
        (
            "a.js",
            "one\nconst re = /a\\/*/;\nCHECK_TOKEN();\nconst end = /x*/;\ntwo\n",
            "one\nconst re = /a\\/*/;\nconst end = /x*/;\ntwo\n",
        ),
        ("a.js", "one\nconst re = /[//]/;\nCHECK_TOKEN();\ntwo\n", "one\nconst re = /[//]/;\ntwo\n"),
        ("a.go", "//go:build linux\n\none\nCHECK_TOKEN()\ntwo\n", "one\nCHECK_TOKEN()\ntwo\n"),
        (
            "a.go",
            'one\nimport (\n\t// int check_token(void) { return deny(); }\n\t"C"\n)\ntwo\n',
            'one\nimport (\n\t"C"\n)\ntwo\n',
        ),
    ],
    ids=[
        "go-block-spans-code",
        "go-inline-blocks",
        "py-trailing-comment",
        "control-js-plain-regex",
        "control-go-cgo-single-import",
        "js-regex-block-open",
        "js-regex-slashes",
        "go-build-tag-dropped",
        "go-cgo-grouped-import",
    ],
)
def test_10f_dropped_code_is_refused(tmp_path, name, upstream_text, fork_text):
    config, fix = comment_world(tmp_path, upstream_text, fork_text, name)
    result = run(config, "--today", "2026-10-27")
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"`{fix[:12]}` {NOT_CARRIED}" in result.stdout, result.stdout


def test_10f_go_build_tags_with_a_comment_only_hunk_is_accepted(tmp_path):
    config, _ = comment_world(
        tmp_path,
        "//go:build linux\n\none\n// why\nCHECK_TOKEN()\ntwo\n",
        "//go:build linux\n\none\nCHECK_TOKEN()\ntwo\n",
    )
    result = run(config, "--today", "2026-10-27")
    assert result.returncode == 0, result.stdout + result.stderr


def test_10f_moved_block_under_a_same_named_function_in_an_unbuilt_file_is_refused(tmp_path):
    base_files = {"a.go": "one\nTOKEN_OK\ntwo\n", "b.go": "func handler() {\n\tthree\n\tfour\n}\n"}
    fix_files = {
        "a.go": "one\nTOKEN_OK\nCHECK_TOKEN\ntwo\n",
        "b.go": "func handler() {\n\tthree\n\tCHECK_SCOPE\n\tfour\n}\n",
    }
    steps = [
        ("token", {"a.go": "one\nTOKEN_OK\nCHECK_TOKEN\ntwo\n"}),
        ("move", {"c.go": "//go:build ignore\n\npackage dead\n\nfunc handler() {\n\tCHECK_SCOPE\n}\n"}),
    ]
    config, _ = hostile_world(
        tmp_path, base_files, fix_files, steps, ["token"], " missing `b.go:+3` because <move> moved it to `c.go`;"
    )
    result = run(config, "--today", "2026-10-27")
    assert result.returncode == 1, result.stdout + result.stderr


def test_10f_release_that_only_renamed_the_block_does_not_excuse_a_fork_drop(tmp_path):
    upstream = new_repo(tmp_path / "upstream")
    base = commit(upstream, "base", {"a.py": "one\ntwo\n", "b.py": "three\nfour\n"})
    git(upstream, "checkout", "-q", "-b", "squash", base)
    git(upstream, "mv", "b.py", "b2.py")
    squash = commit(
        upstream, "squash: rename", {"a.py": "one\nCHECK_TOKEN\ntwo\n", "b2.py": "three\nCHECK_SCOPE\nfour\n"}
    )
    git(upstream, "tag", "v1.2.0")
    git(upstream, "checkout", "-q", "main")
    fix = commit(
        upstream, "fix: token and scope", {"a.py": "one\nCHECK_TOKEN\ntwo\n", "b.py": "three\nCHECK_SCOPE\nfour\n"}
    )
    fork = tmp_path / "fork"
    git(tmp_path, "clone", "-q", str(upstream), str(fork))
    for key, value in (("user.name", "Test"), ("user.email", "test@example.com"), ("commit.gpgsign", "false")):
        git(fork, "config", key, value)
    git(fork, "reset", "-q", "--hard", squash)
    drop = commit(fork, "fork: drop scope", {"b2.py": "three\nfour\n"})
    panel = new_repo(tmp_path / "panel")
    forks = panel / "upstream-forks"
    forks.mkdir()
    (forks / "forks.toml").write_text(f"""
[[repo]]
name = "kit"
mode = "ledger"
fork = "Owner/kit"
upstream = "Up/kit"
fork_url = "{fork}"
upstream_url = "{upstream}"
ported_ref = "main"
grandfathered_base = "{base}"
ledger = "upstream-forks/kit.md"
reason = "no tracker of its own"
""")
    (forks / "kit.md").write_text(
        f"## Ported by design\n\n- `{fix}` ported in fork commits {squash} and {drop}; missing `b.py:+2` because {squash} "
        "reworked it; security: yes, it checks tokens\n"
    )
    result = run(forks / "forks.toml", "--today", "2026-10-27")
    assert result.returncode == 1, result.stdout + result.stderr


@pytest.fixture
def inventory(monkeypatch):
    spec = importlib.util.spec_from_file_location("upstream_inventory_10f", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    known = {SIDE: SIDE}
    monkeypatch.setattr(module, "resolve_commit", lambda token: known.get(token))
    monkeypatch.setattr(module, "is_ancestor", lambda ancestor, descendant: False)
    monkeypatch.setattr(module, "on_upstream_ref", lambda sha: False)
    return module


@pytest.mark.parametrize(
    "reason",
    [
        f"ported in fork commit {GHOST[:12]}; it never left branch `nowhere` security: no",
        f"ported in fork commit {GHOST[:12]} which no branch `x` holds security: no",
        f"ported in fork commit {GHOST[:12]} (see fork/none) security: no",
        f"ported in {SIDE[:12]} on the fork, not on main yet security: no",
        f"ported in commit {GHOST[:12]} security: no",
    ],
    ids=[
        "ghost-branch-word-same-clause",
        "ghost-no-branch",
        "ghost-fork-slash",
        "side-without-fork-commit-wording",
        "ghost-commit-wording",
    ],
)
def test_10f_unverifiable_fork_cite_is_flagged(inventory, reason):
    problems = inventory.cited_history_problems(
        {"where": "kit.md:3", "token": SELF, "reason": reason}, "main", frozenset({SELF})
    )
    assert problems, reason
