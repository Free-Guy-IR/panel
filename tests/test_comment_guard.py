import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import comment_guard

UPSTREAM_CORE_OLD = 'def run(value):\n    # upstream note\n    """Upstream doc."""\n    return value\n'
UPSTREAM_CORE = 'def run(value):\n    # upstream note, reworded\n    """Upstream doc."""\n    return value\n'
UPSTREAM_OTHER = "def other():\n    # shared upstream wording\n    return 1\n"
FORK_FEATURE = (
    "def feature(value):\n    try:\n        return int(value)\n    except ValueError, TypeError:\n        return 0\n"
)
ZERO = "0" * 40


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=Free-Guy-IR", "-c", "user.email=guard@example.invalid", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repo: Path, files: dict[str, str | None], message: str) -> str:
    for name, content in files.items():
        target = repo / name
        if content is None:
            target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _commit(root, {"app/core.py": UPSTREAM_CORE_OLD, "app/other.py": UPSTREAM_OTHER}, "upstream one")
    upstream = _commit(root, {"app/core.py": UPSTREAM_CORE}, "upstream two")
    _git(root, "tag", "upstream-release", upstream)
    _git(root, "branch", "upstream-dev", upstream)
    base = _commit(root, {"app/fork/feature.py": FORK_FEATURE}, "fork base")
    _git(root, "update-ref", "refs/remotes/origin/main", base)
    return root


def _guard(repo: Path, base: str | None, head: str, *extra: str) -> int:
    argv = ["--repo", str(repo), "--head", head, "--upstream", "upstream-release", "--upstream", "upstream-dev"]
    if base is not None:
        argv += ["--base", base]
    return comment_guard.main([*argv, *extra])


def _feature_with(line: str) -> str:
    return FORK_FEATURE.replace("    try:\n", f"{line}\n    try:\n", 1)


def test_a_new_comment_in_a_fork_file_fails_and_names_its_line(repo, capsys):
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, {"app/fork/feature.py": _feature_with("    # explain")}, "add a comment")

    assert _guard(repo, base, head) == 1
    assert "app/fork/feature.py:2: new comment that upstream does not have: # explain" in capsys.readouterr().out


def test_a_new_docstring_fails_even_in_a_file_only_python_3_14_can_parse(repo, capsys):
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, {"app/fork/feature.py": _feature_with('    """Explain the feature."""')}, "add a docstring")

    assert _guard(repo, base, head) == 1
    assert "new docstring that upstream does not have: Explain the feature." in capsys.readouterr().out


def test_a_trailing_comment_and_an_attribute_docstring_fail(repo, capsys):
    base = _git(repo, "rev-parse", "HEAD")
    source = "LIMIT = 3  # three tries\n'''Why three.'''\n"
    head = _commit(repo, {"app/fork/limits.py": source}, "add a module")

    assert _guard(repo, base, head) == 1
    out = capsys.readouterr().out
    assert "app/fork/limits.py:1: new comment that upstream does not have: # three tries" in out
    assert "app/fork/limits.py:2: new docstring that upstream does not have: Why three." in out


@pytest.mark.parametrize(
    "line",
    [
        "    # noqa: F401",
        "    # type: ignore[attr-defined]",
        "    # pragma: no cover",
        "    # fmt: off",
        "    # pyright: ignore",
    ],
)
def test_functional_pragmas_pass(repo, line):
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, {"app/fork/feature.py": _feature_with(line)}, "add a pragma")

    assert _guard(repo, base, head) == 0


def test_a_shebang_passes(repo):
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, {"scripts/tool.sh": "#!/usr/bin/env bash\necho ok\n"}, "add a script")

    assert _guard(repo, base, head) == 0


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("app/core.py", UPSTREAM_CORE.replace("return value", "return value * 2")),
        ("app/core.py", UPSTREAM_CORE_OLD),
        ("app/fork/feature.py", _feature_with("    # shared upstream wording")),
    ],
    ids=["same-file", "older-upstream-version", "copied-from-another-upstream-file"],
)
def test_text_upstream_already_has_passes(repo, name, content):
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, {name: content}, "reuse upstream text")

    assert _guard(repo, base, head) == 0


def test_a_comment_brought_in_by_a_merge_or_added_while_merging_fails(repo, capsys):
    base = _git(repo, "rev-parse", "HEAD")
    side = _commit(repo, {"app/fork/side.py": "def side():\n    # from the side branch\n    return 1\n"}, "side")
    _git(repo, "checkout", "-q", "--detach", base)
    _commit(repo, {"app/fork/main_only.py": "VALUE = 1\n"}, "main side")
    _git(repo, "merge", "-q", "--no-ff", "--no-commit", side)
    (repo / "app/fork/main_only.py").write_text("VALUE = 1  # chosen while resolving\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "merge")
    merge = _git(repo, "rev-parse", "HEAD")

    assert len(_git(repo, "log", "-1", "--format=%P", merge).split()) == 2
    assert _guard(repo, base, merge) == 1
    out = capsys.readouterr().out
    assert "app/fork/side.py:2: new comment" in out
    assert "app/fork/main_only.py:1: new comment" in out


def test_a_comment_added_and_removed_inside_the_range_passes(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, {"app/fork/feature.py": _feature_with("    # explain")}, "add")
    head = _commit(repo, {"app/fork/feature.py": FORK_FEATURE}, "remove")

    assert _guard(repo, base, head) == 0


def test_a_new_branch_with_no_previous_tip_is_checked_from_its_merge_base(repo, capsys):
    head = _commit(repo, {"app/fork/feature.py": _feature_with("    # explain")}, "add a comment")

    assert _guard(repo, ZERO, head, "--fallback-base", "refs/remotes/origin/main") == 1
    assert "# explain" in capsys.readouterr().out


def test_an_existing_comment_that_moves_with_its_file_passes(repo):
    with_comment = _commit(repo, {"app/fork/feature.py": _feature_with("    # explain")}, "an older comment")
    head = _commit(repo, {"app/fork/feature.py": None, "app/fork/renamed.py": _feature_with("    # explain")}, "rename")

    assert _guard(repo, with_comment, head) == 0


def test_generated_files_alembic_templates_and_spec_kit_pass(repo):
    base = _git(repo, "rev-parse", "HEAD")
    migration = (
        '"""add a table\n\nRevision ID: abc123\nRevises: def456\nCreate Date: 2026-11-11\n"""\n'
        "# revision identifiers, used by Alembic.\n"
        'revision = "abc123"\n'
        "def upgrade():\n"
        "    # ### commands auto generated by Alembic - please adjust! ###\n"
        "    pass\n"
        "    # ### end Alembic commands ###\n"
    )
    head = _commit(
        repo,
        {
            "app/db/migrations/versions/abc123_add_a_table.py": migration,
            "dashboard/src/service/api/index.ts": "/** Generated by orval */\nexport {}\n",
            ".specify/scripts/bash/common.sh": "# Spec Kit helper\necho ok\n",
        },
        "generated files",
    )

    assert _guard(repo, base, head) == 0


def test_a_new_comment_in_a_migration_still_fails(repo):
    base = _git(repo, "rev-parse", "HEAD")
    migration = '"""x\n\nRevision ID: abc123\n"""\ndef upgrade():\n    # explain the migration\n    pass\n'
    head = _commit(repo, {"app/db/migrations/versions/abc123_x.py": migration}, "migration")

    assert _guard(repo, base, head) == 1


TYPESCRIPT = os.environ.get("COMMENT_GUARD_TYPESCRIPT")


@pytest.mark.skipif(not TYPESCRIPT or shutil.which("node") is None, reason="needs node and COMMENT_GUARD_TYPESCRIPT")
def test_typescript_comments_are_found_by_the_parser(repo, capsys):
    base = _git(repo, "rev-parse", "HEAD")
    source = (
        "// eslint-disable-next-line no-console\n"
        "const url = 'https://example.invalid/path';\n"
        "export const View = () => <p>// shown as text</p>; // explain the view\n"
    )
    head = _commit(repo, {"dashboard/src/fork/view.tsx": source}, "a view")

    assert _guard(repo, base, head, "--typescript", TYPESCRIPT) == 1
    out = capsys.readouterr().out
    assert "dashboard/src/fork/view.tsx:3: new comment that upstream does not have: // explain the view" in out
    assert out.count("new comment that upstream does not have") == 1


@pytest.mark.skipif(not TYPESCRIPT or shutil.which("node") is None, reason="needs node and COMMENT_GUARD_TYPESCRIPT")
@pytest.mark.parametrize(
    "name", ["dashboard/vite.config.mts", "tools/build.cts", "tools/run.mjs", "tools/run.cjs", "src/App.jsx"]
)
def test_module_and_jsx_script_files_are_scanned_too(repo, capsys, name):
    base = _git(repo, "rev-parse", "HEAD")
    source = "/* eslint-disable */\nexport const config = { port: 1 }; // explain the port\n"
    head = _commit(repo, {name: source}, "a module script")

    assert _guard(repo, base, head, "--typescript", TYPESCRIPT) == 1
    out = capsys.readouterr().out
    assert f"{name}:2: new comment that upstream does not have: // explain the port" in out
    assert out.count("new comment that upstream does not have") == 1
