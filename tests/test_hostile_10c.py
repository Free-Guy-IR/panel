import pytest

from tests.test_upstream_forks import NOT_CARRIED, comment_world, commit, git, new_repo, run


@pytest.mark.parametrize(
    ("name", "upstream_text", "fork_text"),
    [
        ("a.py", 'one\nPOLICY = """\n# deny-root\nallow-all\n"""\ntwo\n', 'one\nPOLICY = """\nallow-all\n"""\ntwo\n'),
        ("a.go", "one\n//go:build linux\nCHECK_TOKEN\ntwo\n", "one\nCHECK_TOKEN\ntwo\n"),
        ("a.go", "one\n//go:embed policy.rego\nvar policy string\ntwo\n", "one\nvar policy string\ntwo\n"),
        ("a.go", "one\nCHECK_TOKEN\n//nolint:gosec\ntwo\n", "one\nCHECK_TOKEN\ntwo\n"),
        ("a.go", "one\n//go:generate protoc --go_out=. auth.proto\nCHECK_TOKEN\ntwo\n", "one\nCHECK_TOKEN\ntwo\n"),
        ("a.go", "one\nq := `\n// deny-root\nallow-all\n`\ntwo\n", "one\nq := `\nallow-all\n`\ntwo\n"),
        ("a.yml", 'one: 1\nmsg: "a\n  # deny-root\n  c"\ntwo: 2\n', 'one: 1\nmsg: "a\n  c"\ntwo: 2\n'),
        ("a.yml", "one: 1\nrun: |\n  # deny-root\n  echo ok\ntwo: 2\n", "one: 1\nrun: |\n  echo ok\ntwo: 2\n"),
        (
            "a.sh",
            "one\ncat > /etc/cloud/x.cfg <<'EOF'\n#cloud-config\nEOF\ntwo\n",
            "one\ncat > /etc/cloud/x.cfg <<'EOF'\nEOF\ntwo\n",
        ),
        (
            "a.sh",
            "one\ncat > /usr/local/bin/g <<'EOF'\n#!/bin/bash\nset -o pipefail\nEOF\ntwo\n",
            "one\ncat > /usr/local/bin/g <<'EOF'\nset -o pipefail\nEOF\ntwo\n",
        ),
        ("a.js", "one\nconst rule = `\n// deny-root\n`\ntwo\n", "one\nconst rule = `\n`\ntwo\n"),
    ],
    ids=[
        "py-hash-in-string",
        "go-build",
        "go-embed",
        "go-nolint",
        "go-generate",
        "go-raw-string",
        "yaml-quoted-scalar",
        "yaml-block-scalar",
        "sh-heredoc-cloud-config",
        "sh-heredoc-shebang",
        "js-template-literal",
    ],
)
def test_hostile_comment_marker_line_that_is_code_is_refused(tmp_path, name, upstream_text, fork_text):
    config, fix = comment_world(tmp_path, upstream_text, fork_text, name)
    result = run(config, "--today", "2026-10-27")
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"`{fix[:12]}` {NOT_CARRIED}" in result.stdout, result.stdout


def hostile_world(tmp_path, base_files, fix_files, fork_steps, cited, exception, squash_files=None):
    upstream = new_repo(tmp_path / "upstream")
    base = commit(upstream, "base", base_files)
    fix = commit(upstream, "fix: check the token and its scope", fix_files)
    git(upstream, "tag", "v1.1.0")
    made = {"base": base, "fix": fix}
    if squash_files is not None:
        git(upstream, "checkout", "-q", "-b", "squash", base)
        made["squash"] = commit(upstream, "squash: release rollup", squash_files)
        git(upstream, "checkout", "-q", "main")
    fork = tmp_path / "fork"
    git(tmp_path, "clone", "-q", str(upstream), str(fork))
    for key, value in (("user.name", "Test"), ("user.email", "test@example.com"), ("commit.gpgsign", "false")):
        git(fork, "config", key, value)
    git(fork, "reset", "-q", "--hard", made.get("squash", base))
    for step, files in fork_steps:
        made[step] = commit(fork, f"fork: {step}", files)
    for step, sha in made.items():
        exception = exception.replace(f"<{step}>", sha)
    names = " and ".join(made[step] for step in cited)
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
        f"## Ported by design\n\n- `{fix}` ported in fork commits {names};{exception} security: yes, it checks tokens\n"
    )
    return forks / "forks.toml", fix


def test_hostile_moved_block_in_a_different_function_is_refused(tmp_path):
    base_files = {"a.go": "one\nTOKEN_OK\ntwo\n", "b.go": "func handler() {\n\tthree\n\tfour\n}\n"}
    fix_files = {
        "a.go": "one\nTOKEN_OK\nCHECK_TOKEN\ntwo\n",
        "b.go": "func handler() {\n\tthree\n\tCHECK_SCOPE\n\tfour\n}\n",
    }
    steps = [
        ("token", {"a.go": "one\nTOKEN_OK\nCHECK_TOKEN\ntwo\n"}),
        (
            "move",
            {"b.go": "", "c.go": "func handler() {\n\tthree\n\tfour\n}\n\nfunc neverCalled() {\n\tCHECK_SCOPE\n}\n"},
        ),
    ]
    config, _ = hostile_world(
        tmp_path, base_files, fix_files, steps, ["token"], " missing `b.go:+3` because <move> moved it to `c.go`;"
    )
    result = run(config, "--today", "2026-10-27")
    assert result.returncode == 1, result.stdout + result.stderr


def test_hostile_upstream_squash_carrier_never_excuses_a_missing_hunk(tmp_path):
    base_files = {"a.py": "one\ntwo\n", "b.py": "three\nfour\n"}
    fix_files = {"a.py": "one\nCHECK_TOKEN\ntwo\n", "b.py": "three\nCHECK_SCOPE\nfour\n"}
    squash_files = {"a.py": "one\nCHECK_TOKEN\ntwo\n", "b.py": "three\nfour\nUNRELATED\n"}
    config, _ = hostile_world(
        tmp_path,
        base_files,
        fix_files,
        [],
        ["squash"],
        " missing `b.py:+2` because <squash> reworked it;",
        squash_files,
    )
    result = run(config, "--today", "2026-10-27")
    assert result.returncode == 1, result.stdout + result.stderr


@pytest.mark.parametrize(("tag_fix", "code"), [(True, 1), (False, 0)], ids=["fix-released", "fix-never-released"])
def test_a_released_squash_without_the_block_excuses_only_a_never_released_fix(tmp_path, tag_fix, code):
    upstream = new_repo(tmp_path / "upstream")
    base = commit(upstream, "base", {"a.py": "one\ntwo\n", "b.py": "three\nfour\n"})
    git(upstream, "checkout", "-q", "-b", "squash", base)
    squash = commit(
        upstream, "squash: release rollup", {"a.py": "one\nCHECK_TOKEN\ntwo\n", "b.py": "three\nfour\nUNRELATED\n"}
    )
    git(upstream, "tag", "v1.2.0")
    git(upstream, "checkout", "-q", "main")
    fix = commit(
        upstream,
        "fix: check the token and its scope",
        {"a.py": "one\nCHECK_TOKEN\ntwo\n", "b.py": "three\nCHECK_SCOPE\nfour\n"},
    )
    if tag_fix:
        git(upstream, "tag", "v1.1.0")
    fork = tmp_path / "fork"
    git(tmp_path, "clone", "-q", str(upstream), str(fork))
    git(fork, "reset", "-q", "--hard", squash)
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
        f"## Ported by design\n\n- `{fix}` ported in fork commits {squash}; missing `b.py:+2` because {squash} "
        "reworked it; security: yes, it checks tokens\n"
    )
    result = run(forks / "forks.toml", "--today", "2026-10-27")
    assert result.returncode == code, result.stdout + result.stderr
