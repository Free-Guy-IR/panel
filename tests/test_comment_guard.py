import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import comment_guard

UPSTREAM_CORE_OLD = 'def run(value):\n    # upstream note\n    """Upstream doc."""\n    return value\n'
UPSTREAM_CORE = 'def run(value):\n    # upstream note, reworded\n    """Upstream doc."""\n    return value\n'
UPSTREAM_OTHER = "def other():\n    # shared upstream wording\n    return 1\n"
FORK_FEATURE = (
    "def feature(value):\n    try:\n        return int(value)\n    except ValueError, TypeError:\n        return 0\n"
)
ZERO = "0" * 40
GONE = "1" * 40
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "upstream-inventory.yml"
EXPRESSION = re.compile(r"\$\{\{\s*([\w.]+)\s*\}\}")
CI_UPSTREAM_REFS = ("refs/upstream-tags/v5.6.0", "refs/remotes/upstream/dev", "refs/remotes/upstream/main")
UPSTREAM_CONFIG_FILES = {
    "Dockerfile": "FROM python:3.12\n# upstream builder note\nRUN true\n",
    "Makefile": "# upstream target note\nrun:\n\ttrue\n",
    "dashboard/src/index.css": "/* upstream theme note */\nbody {\n  margin: 0;\n}\n",
}


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
    _git(repo, "update-ref", "refs/remotes/origin/main", with_comment)
    head = _commit(repo, {"app/fork/feature.py": None, "app/fork/renamed.py": _feature_with("    # explain")}, "rename")

    assert _guard(repo, with_comment, head) == 0


def test_a_comment_that_only_an_earlier_unverified_push_added_is_still_new(repo, capsys):
    first = _commit(repo, {"app/fork/feature.py": _feature_with("    # explain")}, "a push that went red")
    second = _commit(repo, {"app/fork/other.py": "VALUE = 2\n"}, "the push on top of it")

    assert _guard(repo, first, second) == 1
    assert "app/fork/feature.py:2: new comment that upstream does not have: # explain" in capsys.readouterr().out


def test_a_base_that_is_not_in_the_clone_fails_closed_instead_of_falling_back(repo):
    head = _commit(repo, {"app/fork/feature.py": _feature_with("    # explain")}, "force-pushed main")
    _git(repo, "update-ref", "refs/remotes/origin/main", head)

    with pytest.raises(SystemExit, match=f"base {GONE} is not a commit in this clone"):
        _guard(repo, GONE, head)


def test_a_head_already_on_the_trusted_branch_needs_the_grandfathered_base(repo):
    before = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, {"app/fork/other.py": "VALUE = 2\n"}, "a push to main")
    _git(repo, "update-ref", "refs/remotes/origin/main", head)

    with pytest.raises(SystemExit, match="only a grandfathered base is trusted"):
        _guard(repo, before, head)


def test_a_head_already_on_the_trusted_branch_is_checked_from_the_grandfathered_base(repo, capsys):
    grandfathered = _git(repo, "rev-parse", "HEAD")
    first = _commit(repo, {"app/fork/feature.py": _feature_with("    # explain")}, "a push to main that went red")
    head = _commit(repo, {"app/fork/other.py": "VALUE = 2\n"}, "the next push to main")
    _git(repo, "update-ref", "refs/remotes/origin/main", head)

    assert _guard(repo, first, head, "--grandfathered-base", grandfathered) == 1
    out = capsys.readouterr().out
    assert f"comment guard: {grandfathered[:12]}..{head[:12]} (grandfathered base" in out
    assert "app/fork/feature.py:2: new comment that upstream does not have: # explain" in out


def test_a_missing_trusted_branch_fails_closed(repo):
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, {"app/fork/other.py": "VALUE = 2\n"}, "a clean change")

    with pytest.raises(SystemExit, match="trusted branch refs/remotes/origin/gone is not a commit"):
        _guard(repo, base, head, "--fallback-base", "refs/remotes/origin/gone")


def test_a_missing_grandfathered_base_fails_closed_even_off_the_trusted_branch(repo):
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, {"app/fork/other.py": "VALUE = 2\n"}, "a clean change")

    with pytest.raises(SystemExit, match=f"grandfathered base {GONE} is not a commit"):
        _guard(repo, base, head, "--grandfathered-base", GONE)


def test_a_base_from_unrelated_history_fails_closed(repo):
    head = _commit(repo, {"app/fork/other.py": "VALUE = 2\n"}, "a clean change")
    _git(repo, "checkout", "-q", "--orphan", "unrelated")
    unrelated = _commit(repo, {}, "an unrelated root with the same files")

    with pytest.raises(SystemExit, match="share no history"):
        _guard(repo, unrelated, head)


@pytest.mark.parametrize(
    ("name", "content", "number", "comment"),
    [
        ("Dockerfile", "FROM python:3.12\n# explain the base image\nRUN true\n", 2, "# explain the base image"),
        ("Makefile", "run:\n\t# explain the target\n\ttrue\n", 2, "# explain the target"),
        (
            "dashboard/src/index.css",
            "body {\n  /* explain the margin */\n  margin: 0;\n}\n",
            2,
            "/* explain the margin */",
        ),
        (
            "alembic.ini",
            "[alembic]\n# explain the script location\nscript_location = app/db/migrations\n",
            2,
            "# explain the script location",
        ),
        (".env.example", "# explain the port\nUVICORN_PORT=8000\n", 1, "# explain the port"),
    ],
)
def test_comments_in_dockerfiles_makefiles_css_ini_and_env_examples_fail(repo, capsys, name, content, number, comment):
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, {name: content}, "a commented file")

    assert _guard(repo, base, head) == 1
    assert f"{name}:{number}: new comment that upstream does not have: {comment}" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("name", "content", "number", "comment"),
    [
        (
            "deploy/Dockerfile.dev",
            "FROM python:3.12\nRUN apt-get install -y curl # explain curl\n",
            2,
            "# explain curl",
        ),
        ("deploy/node.dockerfile", "FROM node:22\n  # explain the image\n", 2, "# explain the image"),
        ("GNUmakefile", "VERSION = 1 # explain the version\n", 1, "# explain the version"),
        ("tools/rules.mk", "# explain the rules\nall:\n", 1, "# explain the rules"),
        ("dashboard/.env.sample", "API_URL=http://localhost:8000 # explain the url\n", 1, "# explain the url"),
        ("pytest.ini", "[pytest]\n; explain the markers\nmarkers = slow\n", 2, "; explain the markers"),
        (".gitignore", "*.log\n# explain the logs\n", 2, "# explain the logs"),
        ("dashboard/.dockerignore", "node_modules\n  # explain the folder\n", 2, "# explain the folder"),
        ("app/db/migrations/script.py.mako", "${imports}\n## explain the template\n", 2, "## explain the template"),
    ],
)
def test_the_other_config_and_template_files_are_scanned_too(repo, capsys, name, content, number, comment):
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, {name: content}, "a commented config file")

    assert _guard(repo, base, head) == 1
    assert f"{name}:{number}: new comment that upstream does not have: {comment}" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("name", "content"),
    [
        (
            "Dockerfile",
            (
                "# syntax=docker/dockerfile:1.7\n"
                "# check=skip=JSONArgsRecommended\n"
                "FROM python:3.12\n"
                "# hadolint ignore=DL3008\n"
                "RUN apt-get update && apt-get install -y curl\n"
                'ENV GREETING="hello # world"\n'
                "LABEL org.opencontainers.image.url=https://example.test/#readme\n"
            ),
        ),
        (
            "Dockerfile",
            (
                "# syntax=docker/dockerfile:1.7\n"
                "# Copyright (c) 2026 Example Authors\n"
                "# Released to you under the MIT terms\n"
                "# SPDX-License-Identifier: MIT\n"
                "FROM python:3.12\n"
            ),
        ),
        (
            "dashboard/public/statics/fonts/example.css",
            (
                "/*!\n * Example Sans\n * Copyright 2026 Example Authors\n * SPDX-License-Identifier: OFL-1.1\n */\n"
                "body {\n  margin: 0;\n}\n"
            ),
        ),
        (
            "dashboard/src/index.css",
            (
                '.a::before {\n  content: "/* not a comment */";\n}\n'
                ".icon {\n  background: url(images/*/icon.png);\n}\n"
                "/* stylelint-disable-next-line color-no-hex */\n.b {\n  color: #fff;\n}\n"
                "/*# sourceMappingURL=index.css.map */\n"
            ),
        ),
        ("Dockerfile", "\ufeff# syntax=docker/dockerfile:1.7\n# escape=`\nFROM python:3.12\n"),
        ("theme.ini", "[colors]\nbackground = #ffffff\n"),
        (".env.example", 'SUB_URL=https://example.test/#/sub\nGREETING="hello # world"\n'),
        ("Makefile", '#!/usr/bin/make -f\nrun:\n\t@echo "#"\n'),
        ("Dockerfile", "FROM python:3.12\n# shared upstream wording\n"),
    ],
    ids=[
        "dockerfile-directives-pragma-and-quoted-hash",
        "dockerfile-license-header",
        "css-license-header",
        "css-strings-urls-and-pragmas",
        "dockerfile-directives-after-a-byte-order-mark",
        "ini-value-with-a-hash",
        "env-values-with-a-hash",
        "makefile-shebang-and-quoted-hash",
        "dockerfile-line-copied-from-upstream",
    ],
)
def test_functional_lines_and_non_comments_in_the_newly_scanned_files_pass(repo, capsys, name, content):
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, {name: content}, "functional lines only")

    assert _guard(repo, base, head) == 0, capsys.readouterr().out


@pytest.mark.parametrize(
    ("name", "content", "number", "comment"),
    [
        ("Dockerfile", "FROM python:3.12\n# syntax=docker/dockerfile:1.7\n", 2, "# syntax=docker/dockerfile:1.7"),
        (
            "dashboard/src/index.css",
            "/*! Copyright 2026 Example Authors */\nbody {\n  /* explain the margin */\n  margin: 0;\n}\n",
            3,
            "/* explain the margin */",
        ),
    ],
    ids=["a-directive-after-an-instruction-is-a-comment", "a-license-header-does-not-cover-later-comments"],
)
def test_the_functional_line_exceptions_stay_narrow(repo, capsys, name, content, number, comment):
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, {name: content}, "a comment next to a functional line")

    assert _guard(repo, base, head) == 1
    out = capsys.readouterr().out
    assert f"{name}:{number}: new comment that upstream does not have: {comment}" in out
    assert out.count("new comment that upstream does not have") == 1


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


@pytest.mark.skipif(not TYPESCRIPT or shutil.which("node") is None, reason="needs node and COMMENT_GUARD_TYPESCRIPT")
@pytest.mark.parametrize("name", ["dashboard/tsconfig.app.json", "PasarGuard.code-workspace"])
def test_jsonc_comments_in_tsconfig_and_workspace_files_fail(repo, capsys, name):
    base = _git(repo, "rev-parse", "HEAD")
    source = (
        '{\n  "compilerOptions": {\n    "baseUrl": ".", // explain the base url\n'
        '    "paths": { "@/*": ["./src/*"] }\n  }\n}\n'
    )
    head = _commit(repo, {name: source}, "a commented config")

    assert _guard(repo, base, head, "--typescript", TYPESCRIPT) == 1
    out = capsys.readouterr().out
    assert f"{name}:3: new comment that upstream does not have: // explain the base url" in out
    assert out.count("new comment that upstream does not have") == 1


def test_the_recorded_grandfathered_base_is_a_full_commit_on_this_history():
    recorded = (REPO_ROOT / ".comment-guard-base").read_text()

    assert re.fullmatch(r"[0-9a-f]{40}\n", recorded)
    ancestor = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "merge-base", "--is-ancestor", recorded.strip(), "HEAD"], check=False
    )
    assert ancestor.returncode == 0


def _workflow_step(name: str) -> tuple[dict[str, str], str]:
    lines = WORKFLOW.read_text().splitlines()
    start = lines.index(f"      - name: {name}")
    end = next((index for index in range(start + 1, len(lines)) if lines[index].startswith("      - ")), len(lines))
    env, script, section = {}, [], None
    for line in lines[start + 1 : end]:
        if line.startswith("          ") or not line.strip():
            if section == "env" and line.strip():
                key, value = line.strip().split(": ", 1)
                env[key] = value
            elif section == "run":
                script.append(line[10:])
            continue
        key, _, value = line.strip().partition(":")
        section = key if key in ("env", "run") else None
        assert section != "run" or value.strip() == "|"
    assert script, f"the {name!r} step has no run block"
    return env, "\n".join(script).strip("\n") + "\n"


def _expand(text: str, context: dict[str, str]) -> str:
    return EXPRESSION.sub(lambda match: context[match.group(1)], text)


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / ".git" / "info").mkdir(exist_ok=True)
    (root / ".git" / "info" / "exclude").write_text("/scripts/comment_guard.py\n/scripts/comment_guard_ts.js\n")
    upstream = _commit(
        root, {"app/core.py": UPSTREAM_CORE, "app/other.py": UPSTREAM_OTHER, **UPSTREAM_CONFIG_FILES}, "upstream"
    )
    for ref in CI_UPSTREAM_REFS:
        _git(root, "update-ref", ref, upstream)
    grandfathered = _commit(
        root,
        {"app/fork/feature.py": FORK_FEATURE, "app/fork/legacy.py": "LEGACY = 1  # grandfathered note\n"},
        "the fork before the guard",
    )
    main = _commit(root, {".comment-guard-base": f"{grandfathered}\n"}, "record the grandfathered base")
    _git(root, "update-ref", "refs/remotes/origin/main", main)
    (root / "scripts").mkdir()
    for name in ("comment_guard.py", "comment_guard_ts.js"):
        shutil.copy2(REPO_ROOT / "scripts" / name, root / "scripts" / name)
    return root


def _ci_step(checkout: Path, tmp_path: Path, event: str, head: str, before: str, ref: str):
    _git(checkout, "checkout", "-q", "--detach", head)
    runner_temp = tmp_path / "runner-temp"
    tools = tmp_path / "tools"
    runner_temp.mkdir(exist_ok=True)
    tools.mkdir(exist_ok=True)
    shim = tools / "python"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    shim.chmod(0o755)
    context = {
        "github.event_name": event,
        "github.event.before": before,
        "github.ref": ref,
        "github.sha": head,
        "runner.temp": str(runner_temp),
    }
    env_spec, script = _workflow_step("Comment guard")
    env = {**os.environ, "RUNNER_TEMP": str(runner_temp), "PATH": f"{tools}{os.pathsep}{os.environ.get('PATH', '')}"}
    env.update({key: _expand(value, context) for key, value in env_spec.items()})
    step = tmp_path / "comment-guard-step.sh"
    step.write_text(_expand(script, context))
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(step)],
        cwd=checkout,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _push(checkout: Path, tmp_path: Path, head: str, before: str, branch: str):
    _git(checkout, "update-ref", f"refs/remotes/origin/{branch}", head)
    return _ci_step(checkout, tmp_path, "push", head, before, f"refs/heads/{branch}")


def _pull_request(checkout: Path, tmp_path: Path, branch_head: str):
    _git(checkout, "checkout", "-q", "--detach", "refs/remotes/origin/main")
    _git(checkout, "merge", "-q", "--no-ff", "--no-edit", branch_head)
    return _ci_step(checkout, tmp_path, "pull_request", _git(checkout, "rev-parse", "HEAD"), "", "refs/pull/1/merge")


def _red_commit(checkout: Path) -> str:
    return _commit(checkout, {"app/fork/feature.py": _feature_with("    # explain")}, "a push with a comment")


@pytest.mark.parametrize("branch", ["main", "lane"])
def test_ci_a_push_on_top_of_a_red_push_still_fails_while_the_comment_is_on_the_tip(checkout, tmp_path, branch):
    before = _git(checkout, "rev-parse", "HEAD")
    first = _red_commit(checkout)
    assert _push(checkout, tmp_path, first, before, branch).returncode == 1
    second = _commit(checkout, {"app/fork/other.py": "VALUE = 2\n"}, "the push on top of it")

    result = _push(checkout, tmp_path, second, first, branch)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "app/fork/feature.py:2: new comment that upstream does not have: # explain" in result.stdout


def test_ci_a_force_push_to_main_whose_old_tip_is_gone_still_checks_the_tip(checkout, tmp_path):
    main = _git(checkout, "rev-parse", "HEAD")
    _git(checkout, "checkout", "-q", "--detach", main)
    gone = _commit(checkout, {"app/fork/other.py": "VALUE = 2\n"}, "the tip the force push replaces")
    _git(checkout, "checkout", "-q", "--detach", main)
    head = _red_commit(checkout)
    _git(checkout, "reflog", "expire", "--expire=now", "--all")
    _git(checkout, "gc", "-q", "--prune=now")
    probe = subprocess.run(["git", "-C", str(checkout), "cat-file", "-e", gone], capture_output=True, check=False)
    assert probe.returncode != 0

    result = _push(checkout, tmp_path, head, gone, "main")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "app/fork/feature.py:2: new comment that upstream does not have: # explain" in result.stdout


def test_ci_a_clean_push_to_main_passes_and_leaves_grandfathered_and_upstream_comments_alone(checkout, tmp_path):
    before = _git(checkout, "rev-parse", "HEAD")
    head = _commit(
        checkout,
        {
            "app/fork/other.py": "VALUE = 2\n",
            "Dockerfile": UPSTREAM_CONFIG_FILES["Dockerfile"] + "RUN echo ready\n",
            "Makefile": UPSTREAM_CONFIG_FILES["Makefile"] + "test:\n\ttrue\n",
            "dashboard/src/index.css": UPSTREAM_CONFIG_FILES["dashboard/src/index.css"] + ".b {\n  color: red;\n}\n",
        },
        "a clean push that edits upstream config files",
    )

    result = _push(checkout, tmp_path, head, before, "main")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "(grandfathered base, as" in result.stdout


def test_ci_a_new_branch_cut_from_main_passes(checkout, tmp_path):
    head = _git(checkout, "rev-parse", "HEAD")

    result = _push(checkout, tmp_path, head, ZERO, "lane")

    assert result.returncode == 0, result.stdout + result.stderr


def test_ci_a_pull_request_is_checked_against_main(checkout, tmp_path):
    main = _git(checkout, "rev-parse", "HEAD")
    clean = _commit(checkout, {"app/fork/other.py": "VALUE = 2\n"}, "a clean change")
    assert _pull_request(checkout, tmp_path, clean).returncode == 0
    _git(checkout, "checkout", "-q", "--detach", main)
    red = _red_commit(checkout)

    result = _pull_request(checkout, tmp_path, red)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "app/fork/feature.py:2: new comment that upstream does not have: # explain" in result.stdout


@pytest.mark.parametrize(
    ("recorded", "branch", "message"),
    [
        (None, "main", ""),
        ("not a commit\n", "main", ""),
        (f"{GONE}\n", "main", f"grandfathered base {GONE} is not a commit"),
        (f"{GONE}\n", "lane", f"grandfathered base {GONE} is not a commit"),
    ],
    ids=["file-missing", "not-a-sha", "commit-not-in-the-clone-on-main", "commit-not-in-the-clone-on-a-branch"],
)
def test_ci_the_step_fails_closed_without_a_usable_grandfathered_base(checkout, tmp_path, recorded, branch, message):
    before = _git(checkout, "rev-parse", "HEAD")
    head = _commit(checkout, {".comment-guard-base": recorded, "app/fork/other.py": "VALUE = 2\n"}, "a clean push")

    result = _push(checkout, tmp_path, head, before, branch)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "no new comments or docstrings" not in result.stdout
    assert message in result.stderr


def test_ci_the_step_fails_closed_when_main_is_not_in_the_clone(checkout, tmp_path):
    before = _git(checkout, "rev-parse", "HEAD")
    head = _commit(checkout, {"app/fork/other.py": "VALUE = 2\n"}, "a clean push")
    _git(checkout, "update-ref", "-d", "refs/remotes/origin/main")

    result = _ci_step(checkout, tmp_path, "push", head, before, "refs/heads/lane")

    assert result.returncode != 0, result.stdout + result.stderr
    assert "trusted branch refs/remotes/origin/main is not a commit in this clone" in result.stderr
