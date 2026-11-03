import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "upstream_forks.py"
ENV = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")} | {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(repo, *args):
    result = subprocess.run(["git", *args], cwd=repo, env=ENV, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def new_repo(path, branch="main"):
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", branch)
    for key, value in (("user.name", "Test"), ("user.email", "test@example.com"), ("commit.gpgsign", "false")):
        git(path, "config", key, value)
    return path


def commit(repo, message, files):
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    git(repo, "add", *files)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def world(tmp_path):
    upstream = new_repo(tmp_path / "upstream")
    root = commit(upstream, "root", {"a.txt": "a\n"})
    fixed = commit(upstream, "upstream fix", {"a.txt": "a fixed\n"})
    bumped = commit(upstream, "upstream version bump", {"version.txt": "2\n"})
    git(upstream, "tag", "v2.0.0")
    git(upstream, "checkout", "-q", "-b", "feature", root)
    feature = commit(upstream, "upstream feature", {"f.txt": "f\n"})
    git(upstream, "checkout", "-q", "main")

    fork = new_repo(tmp_path / "fork")
    commit(fork, "fork root", {"a.txt": "a\n"})
    commit(fork, f"port the fix\n\nUpstream: {fixed}", {"a.txt": "a fixed\n"})
    commit(fork, "fork tracker", {".github/workflows/upstream-drift.yml": "on: push\n"})

    panel = new_repo(tmp_path / "panel")
    (panel / "upstream-forks").mkdir()
    (panel / "scripts").mkdir()
    (panel / "scripts" / "tracker.yml").write_text("on: push\n")
    return {
        "tmp": tmp_path,
        "upstream": upstream,
        "fork": fork,
        "panel": panel,
        "shas": {"root": root, "fixed": fixed, "bumped": bumped, "feature": feature},
    }


def write_map(world, text, ledger="## Ported by design\n\n## Waived\n"):
    forks = world["panel"] / "upstream-forks"
    (forks / "forks.toml").write_text(text)
    (forks / "kit.md").write_text(ledger)
    return forks / "forks.toml"


def ledger_map(world, extra=""):
    return f"""
[[repo]]
name = "kit"
mode = "ledger"
fork = "Owner/kit"
upstream = "Up/kit"
fork_url = "{world["fork"]}"
upstream_url = "{world["upstream"]}"
ported_ref = "main"
grandfathered_base = "{world["shas"]["root"]}"
ledger = "upstream-forks/kit.md"
reason = "no tracker of its own"
{extra}
"""


def run(config, *args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--config", str(config), "--skip-images", *args],
        env=ENV,
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_fork_without_shared_history_reports_only_the_unported_upstream_commits(world):
    result = run(write_map(world, ledger_map(world)))
    assert result.returncode == 1, result.stdout + result.stderr
    shas = world["shas"]
    assert shas["bumped"][:12] in result.stdout
    assert shas["feature"][:12] in result.stdout
    assert shas["fixed"][:12] not in result.stdout.split("Unported upstream commits", 1)[1]
    assert shas["root"][:12] not in result.stdout.split("Unported upstream commits", 1)[1]


def test_waived_upstream_commits_make_the_fork_green(world):
    shas = world["shas"]
    ledger = (
        "## Waived\n\n"
        f"- `{shas['bumped']}` the fork versions itself security: no\n"
        f"- `{shas['feature']}` pending: tracked feature branch security: no until: 2026-10-25\n"
    )
    config = write_map(world, ledger_map(world), ledger)
    assert run(config, "--today", "2026-10-01").returncode == 0
    expired = run(config, "--today", "2026-10-26")
    assert expired.returncode == 1
    assert "was decided only until 2026-10-25" in expired.stdout


def test_the_refs_snapshot_of_a_ledger_fork_is_written_and_enforced(world):
    shas = world["shas"]
    ledger = "## Waived\n\n" + "".join(f"- `{shas[k]}` reason security: no\n" for k in ("bumped", "feature"))
    config = write_map(world, ledger_map(world, 'snapshot = "upstream-forks/kit.refs"'), ledger)
    unrecorded = run(config)
    assert unrecorded.returncode == 1
    assert "is not recorded in" in unrecorded.stdout
    assert run(config, "--write-refs-snapshot").returncode == 0
    assert f"{shas['feature']} upstream/feature" in (world["panel"] / "upstream-forks" / "kit.refs").read_text()
    assert run(config).returncode == 0


def workflow_map(world, extra=""):
    return f"""
[[repo]]
name = "node"
mode = "workflow"
fork = "Owner/node"
upstream = "Up/node"
fork_url = "{world["fork"]}"
tracker_branch = "main"
tracker_path = "{{path}}"
reason = "tracks itself"
{extra}
"""


@pytest.mark.parametrize(
    ("path", "extra", "today", "code", "message"),
    [
        (".github/workflows/upstream-drift.yml", "", "2026-10-01", 0, "OK: .github/workflows/upstream-drift.yml"),
        (".github/workflows/missing.yml", "", "2026-10-01", 1, "FAILED: .github/workflows/missing.yml"),
        (
            ".github/workflows/missing.yml",
            'pending_until = "2026-10-05"\npending_reason = "on the release branch"',
            "2026-10-01",
            0,
            "PENDING until 2026-10-05",
        ),
        (
            ".github/workflows/missing.yml",
            'pending_until = "2026-10-05"\npending_reason = "on the release branch"',
            "2026-10-06",
            1,
            "expired on 2026-10-05",
        ),
    ],
)
def test_an_external_tracker_must_be_published_or_pending_with_a_date(world, path, extra, today, code, message):
    config = write_map(world, workflow_map(world, extra).replace("{path}", path))
    result = run(config, "--today", today)
    assert result.returncode == code, result.stdout + result.stderr
    assert message in result.stdout


def test_every_fork_of_the_owner_must_be_in_the_map(world):
    config = write_map(world, workflow_map(world).replace("{path}", ".github/workflows/upstream-drift.yml"))
    repos = world["tmp"] / "repos.json"
    repos.write_text(json.dumps([{"name": "node", "fork": True}, {"name": "extra", "fork": True}, {"name": "own"}]))
    result = run(config, "--owner", "Owner", "--repos-file", str(repos))
    assert result.returncode == 1
    assert "Owner/extra has no entry and therefore no tracker" in result.stdout
    assert "Owner/own" not in result.stdout


def test_a_released_image_without_an_upstream_git_tag_fails(world):
    config = write_map(
        world,
        workflow_map(world).replace("{path}", ".github/workflows/upstream-drift.yml")
        + f'\n[[image]]\nname = "ghcr.io/up/kit"\nupstream = "Up/kit"\nupstream_url = "{world["upstream"]}"\n',
    )
    tags = world["tmp"] / "tags.json"
    tags.write_text(json.dumps({"ghcr.io/up/kit": ["v2.0.0", "v2.0.0-amd64", "latest", "dev"]}))
    base = [sys.executable, str(SCRIPT), "--config", str(config), "--image-tags-file", str(tags)]
    ok = subprocess.run(base, env=ENV, capture_output=True, text=True, check=False)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "1 release tags, 0 without an upstream git tag" in ok.stdout
    tags.write_text(json.dumps({"ghcr.io/up/kit": ["v2.0.0", "v3.0.1", "v3.0.1-arm64", "latest"]}))
    missing = subprocess.run(base, env=ENV, capture_output=True, text=True, check=False)
    assert missing.returncode == 1
    assert "v3.0.1 is published as an image but has no git tag" in missing.stdout


def test_a_map_entry_without_a_reason_is_rejected(world):
    config = write_map(world, ledger_map(world).replace('reason = "no tracker of its own"', ""))
    result = run(config)
    assert result.returncode == 1
    assert "repo kit has no reason" in result.stdout


def covering_side_ledger(world, feature_reason):
    shas = world["shas"]
    return f"## Waived\n\n- `{shas['bumped']}` the fork versions itself security: no\n- `{shas['feature']}` {feature_reason} security: no\n"


def test_release_refs_decide_when_a_conditional_waiver_ends(world):
    ledger = covering_side_ledger(world, "UNMERGED-WIP: only on upstream feature until: 2026-10-25")
    default = run(write_map(world, ledger_map(world), ledger), "--today", "2026-10-06")
    assert default.returncode == 0, default.stdout + default.stderr
    merged = run(
        write_map(world, ledger_map(world, 'release_refs = ["refs/remotes/upstream/feature"]'), ledger),
        "--today",
        "2026-10-06",
    )
    assert merged.returncode == 1, merged.stdout + merged.stderr
    assert "is waived only while unmerged, but upstream now carries it on upstream/feature" in merged.stdout


def test_a_release_ref_that_matches_nothing_fails(world):
    ledger = covering_side_ledger(world, "UNMERGED-WIP: only on upstream feature until: 2026-10-25")
    config = write_map(world, ledger_map(world, 'release_refs = ["refs/remotes/upstream/master"]'), ledger)
    result = run(config, "--today", "2026-10-06")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "release_refs refs/remotes/upstream/master name no upstream ref" in result.stdout


def test_release_refs_must_be_a_list_of_refs(world):
    result = run(write_map(world, ledger_map(world, 'release_refs = "master"')))
    assert result.returncode == 1
    assert "repo kit release_refs must be a non-empty list of patterns under" in result.stdout


def test_a_repo_can_only_be_covered_by_a_repo_that_tracks_something(world):
    extra = """
[[repo]]
name = "own"
mode = "original"
fork = "Owner/own"
reason = "written for the fork"

[[repo]]
name = "dep"
mode = "covered-by"
fork = "Owner/dep"
upstream = "Up/dep"
covered_by = "own"
reason = "pinned by own"
"""
    result = run(write_map(world, ledger_map(world) + extra))
    assert result.returncode == 1
    assert "repo dep is covered by own, which tracks nothing itself (original)" in result.stdout


def test_a_pending_tracker_needs_a_near_date(world):
    extra = 'pending_until = "2026-12-01"\npending_reason = "on the release branch"'
    config = write_map(world, workflow_map(world, extra).replace("{path}", ".github/workflows/missing.yml"))
    result = run(config, "--today", "2026-10-01")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "pending_until 2026-12-01 is more than 30 days ahead" in result.stdout


@pytest.mark.parametrize(
    ("today", "code", "message"),
    [
        ("2026-10-06", 0, "Upstream parity gate OK, NOT at parity"),
        ("2026-10-14", 1, "was decided only until 2026-10-13"),
    ],
)
def test_a_side_repo_port_queue_is_owed_until_its_deadline(world, today, code, message):
    shas = world["shas"]
    ledger = (
        f"## Waived\n\n- `{shas['feature']}` UNMERGED-WIP: only on upstream feature security: no until: 2026-10-25\n\n"
        f"## Port queue\n\n- `{shas['bumped']}` the version bump lands with the next fork release security: no until: 2026-10-13\n"
    )
    result = run(write_map(world, ledger_map(world), ledger), "--today", today)
    assert result.returncode == code, result.stdout + result.stderr
    assert message in result.stdout


def test_release_refs_must_name_upstream_refs(world):
    result = run(write_map(world, ledger_map(world, 'release_refs = ["refs/remotes/fork/main"]')))
    assert result.returncode == 1
    assert "release_refs must be a non-empty list of patterns under refs/remotes/upstream/ or refs/upstream-tags/" in (
        result.stdout
    )


def test_a_release_glob_without_matches_is_allowed(world):
    ledger = covering_side_ledger(world, "UNMERGED-WIP: only on upstream feature until: 2026-10-25")
    extra = 'release_refs = ["refs/remotes/upstream/main", "refs/upstream-tags/nightly-*"]'
    result = run(write_map(world, ledger_map(world, extra), ledger), "--today", "2026-10-06")
    assert result.returncode == 0, result.stdout + result.stderr


def test_release_refs_belong_only_to_a_ledger_repo(world):
    config = write_map(
        world,
        workflow_map(world, 'release_refs = ["refs/remotes/upstream/main"]').replace(
            "{path}", ".github/workflows/upstream-drift.yml"
        ),
    )
    result = run(config)
    assert result.returncode == 1
    assert "repo node sets release_refs, which only a ledger repo uses" in result.stdout


def test_a_refs_snapshot_line_is_never_passed_to_git_as_an_option(world, tmp_path):
    shas = world["shas"]
    ledger = "## Waived\n\n" + "".join(f"- `{shas[k]}` reason security: no\n" for k in ("bumped", "feature"))
    config = write_map(world, ledger_map(world, 'snapshot = "upstream-forks/kit.refs"'), ledger)
    marker = tmp_path / "PWNED"
    (world["panel"] / "upstream-forks" / "kit.refs").write_text(
        f"--upload-pack=touch${{IFS}}{marker} upstream/feature\n{shas['feature']} upstream/feature\n"
    )
    result = run(config)
    assert not marker.exists()
    assert result.returncode == 1, result.stdout + result.stderr


@pytest.fixture
def shared(tmp_path):
    upstream = new_repo(tmp_path / "upstream")
    base = commit(upstream, "base", {"a.py": "one\ntwo\n"})
    git(upstream, "tag", "v1.0.0")
    fix = commit(upstream, "fix: check the token", {"a.py": "one\nCHECK_TOKEN\ntwo\n"})
    git(upstream, "tag", "v1.1.0")
    fork = tmp_path / "fork"
    git(tmp_path, "clone", "-q", str(upstream), str(fork))
    for key, value in (("user.name", "Test"), ("user.email", "test@example.com"), ("commit.gpgsign", "false")):
        git(fork, "config", key, value)
    git(fork, "reset", "-q", "--hard", base)
    commit(fork, "fork feature", {"f.py": "fork\n"})
    panel = new_repo(tmp_path / "panel")
    (panel / "upstream-forks").mkdir()
    return {"tmp": tmp_path, "upstream": upstream, "fork": fork, "panel": panel, "base": base, "fix": fix}


def shared_map(shared, grandfathered=True):
    line = f'grandfathered_base = "{shared["base"]}"\n' if grandfathered else ""
    text = f"""
[[repo]]
name = "kit"
mode = "ledger"
fork = "Owner/kit"
upstream = "Up/kit"
fork_url = "{shared["fork"]}"
upstream_url = "{shared["upstream"]}"
ported_ref = "main"
{line}ledger = "upstream-forks/kit.md"
reason = "no tracker of its own"
"""
    forks = shared["panel"] / "upstream-forks"
    (forks / "forks.toml").write_text(text)
    (forks / "kit.md").write_text("## Waived\n")
    return forks / "forks.toml"


@pytest.mark.parametrize("hide", ["ours", "revert"])
def test_a_side_fork_that_merged_an_upstream_fix_without_its_lines_is_red(shared, hide):
    fork = shared["fork"]
    if hide == "ours":
        git(fork, "merge", "-q", "-s", "ours", "--no-edit", "v1.1.0")
    else:
        git(fork, "merge", "-q", "--no-ff", "--no-edit", "v1.1.0")
        git(fork, "revert", "--no-edit", "-m", "1", "HEAD")
    assert "CHECK_TOKEN" not in (fork / "a.py").read_text()
    result = run(shared_map(shared), "--today", "2026-10-27")
    assert result.returncode == 1, result.stdout + result.stderr
    assert shared["fix"][:12] in result.stdout.split("Unported upstream commits", 1)[1]
    assert f"{shared['fix'][:12]}: merged into refs/remotes/fork/main, but 1 of 1 hunk(s)" in result.stdout


def test_a_side_fork_that_really_merged_the_fix_is_green(shared):
    git(shared["fork"], "merge", "-q", "--no-ff", "--no-edit", "v1.1.0")
    result = run(shared_map(shared), "--today", "2026-10-27")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "merged into the head, every line present   1" in result.stdout


def test_a_ledger_repo_needs_a_grandfathered_upstream_base(shared):
    result = run(shared_map(shared, grandfathered=False))
    assert result.returncode != 0
    assert "repo kit (ledger) needs grandfathered_base" in result.stdout + result.stderr


@pytest.fixture
def unshared(tmp_path):
    upstream = new_repo(tmp_path / "upstream")
    root = commit(upstream, "root", {"a.py": "one\ntwo\n"})
    fix = commit(upstream, "fix: check the token", {"a.py": "one\nCHECK_TOKEN\ntwo\n"})
    fork = new_repo(tmp_path / "fork")
    commit(fork, "fork root", {"a.py": "one\ntwo\n"})
    panel = new_repo(tmp_path / "panel")
    (panel / "upstream-forks").mkdir()
    return {"tmp": tmp_path, "upstream": upstream, "fork": fork, "panel": panel, "base": root, "fix": fix}


@pytest.mark.parametrize(("later", "code"), [(None, 0), ("drop", 1)])
def test_a_side_fork_without_shared_history_is_covered_by_a_full_copy_only(unshared, later, code):
    fork = unshared["fork"]
    commit(fork, "take the token check", {"a.py": "one\nCHECK_TOKEN\ntwo\n"})
    if later == "drop":
        commit(fork, "fork drops the check", {"a.py": "one\ntwo\n"})
    result = run(shared_map(unshared), "--today", "2026-10-27")
    assert result.returncode == code, result.stdout + result.stderr
    if code == 0:
        assert "copied into the head, every line present   1" in result.stdout
    else:
        assert f"{unshared['fix'][:12]}: copied into refs/remotes/fork/main with the same patch-id, but 1 of 1" in (
            result.stdout
        )


def side_fork(tmp_path, name, keep_fix):
    upstream = new_repo(tmp_path / name / "upstream")
    base = commit(upstream, "base", {"a.py": "one\ntwo\n"})
    git(upstream, "tag", "v1.0.0")
    fix = commit(upstream, "fix: check the token", {"a.py": "one\nCHECK_TOKEN\ntwo\n"})
    git(upstream, "tag", "v1.1.0")
    fork = tmp_path / name / "fork"
    git(tmp_path, "clone", "-q", str(upstream), str(fork))
    for key, value in (("user.name", "Test"), ("user.email", "test@example.com"), ("commit.gpgsign", "false")):
        git(fork, "config", key, value)
    git(fork, "reset", "-q", "--hard", base)
    commit(fork, "fork feature", {"f.py": "fork\n"})
    strategy = ("--no-ff",) if keep_fix else ("-s", "ours")
    git(fork, "merge", "-q", *strategy, "--no-edit", "v1.1.0")
    entry = f"""
[[repo]]
name = "{name}"
mode = "ledger"
fork = "Owner/{name}"
upstream = "Up/{name}"
fork_url = "{fork}"
upstream_url = "{upstream}"
ported_ref = "main"
grandfathered_base = "{base}"
ledger = "upstream-forks/{name}.md"
reason = "no tracker of its own"
"""
    return entry, fix


def test_one_run_over_several_side_forks_reads_each_fork_head_from_its_own_mirror(tmp_path):
    green, _ = side_fork(tmp_path, "akit", keep_fix=True)
    red, fix = side_fork(tmp_path, "bkit", keep_fix=False)
    panel = new_repo(tmp_path / "panel")
    forks = panel / "upstream-forks"
    forks.mkdir()
    (forks / "forks.toml").write_text(green + red)
    (forks / "akit.md").write_text("## Waived\n")
    (forks / "bkit.md").write_text("## Waived\n")
    result = run(forks / "forks.toml", "--today", "2026-10-27")
    assert result.returncode == 1, result.stdout + result.stderr
    second = result.stdout.split("=== bkit:", 1)[1]
    assert f"{fix[:12]}: merged into refs/remotes/fork/main, but 1 of 1 hunk(s)" in second


FILLER = "".join(f"step_{index}() {{\n    run {index}\n}}\n\n" for index in range(8))
RESTORE = (
    'extract() {\n    check_size "$1"\n    tar -xf "$1"\n}\n\n'
    + FILLER
    + 'restore() {\n    extract "$1"\n    load_db\n    report\n}\n'
)
RESTORE_SAFE = (
    'extract() {\n    check_size "$1"\n    reject_link_members "$1" || return 1\n    tar -xf "$1"\n}\n\n'
    + FILLER
    + 'restore() {\n    extract "$1"\n    safety_copy || return 1\n    load_db\n    report\n}\n'
)
RESTORE_PARTIAL = (
    'extract() {\n    check_size "$1"\n    tar -xf "$1"\n}\n\n'
    + FILLER
    + 'restore() {\n    extract "$1"\n    safety_copy || return 1\n    load_db\n    report\n}\n'
)
DOC = "# Restore\n\nA restore loads the backup.\n"
DOC_FIRST = "# Restore\n\nA restore refuses link members.\n"
DOC_WORDED = "# Restore\n\nA restore refuses symbolic and hard link members.\n"
DOC_EXCEPTION = " missing `doc.md:+3` because the branch tip reworded it before squash <squash>;"


def squash_world(tmp_path, carried, cite, exception=DOC_EXCEPTION):
    upstream = new_repo(tmp_path / "upstream")
    base = commit(upstream, "base", {"restore.sh": RESTORE, "doc.md": DOC, "other.txt": "one\n"})
    git(upstream, "checkout", "-q", "-b", "fix/restore-safety")
    original = commit(
        upstream,
        "fix(restore): reject symbolic and hard link members of a backup archive",
        {"restore.sh": RESTORE_SAFE, "doc.md": DOC_FIRST},
    )
    tip = commit(upstream, "docs: name both link kinds", {"doc.md": DOC_WORDED})
    git(upstream, "checkout", "-q", "main")
    unrelated = commit(upstream, "chore: tidy", {"other.txt": "two\n"})
    squash = commit(
        upstream,
        "fix(restore): restore safety followups (#35)",
        {"restore.sh": RESTORE_PARTIAL if carried == "partial" else RESTORE_SAFE, "doc.md": DOC_WORDED},
    )
    git(upstream, "tag", "v1.1.0")
    fork = tmp_path / "fork"
    git(tmp_path, "clone", "-q", str(upstream), str(fork))
    for key, value in (("user.name", "Test"), ("user.email", "test@example.com"), ("commit.gpgsign", "false")):
        git(fork, "config", key, value)
    if carried == "absent":
        git(fork, "reset", "-q", "--hard", unrelated)
    if carried == "undone":
        commit(fork, "fork: drop the link check", {"restore.sh": RESTORE_PARTIAL})
    commit(fork, "fork feature", {"f.txt": "fork\n"})
    carrier = {"squash": squash, "unrelated": unrelated}[cite]
    exception = exception.replace("<squash>", squash).replace("<unrelated>", unrelated)
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
        "## Ported by design\n\n"
        f"- `{original}` ALREADY-IN-FORK: pre-squash commit of the deleted branch fix/restore-safety, carried by "
        f"upstream squash {carrier}, an ancestor of fork main;{exception} security: yes, it rejects link members\n"
        f"- `{tip}` ALREADY-IN-FORK: tip of the branch, whose tree is the tree of the squash security: no, wording\n"
    )
    return forks / "forks.toml", original


NO_CARRIER = "is ported by design as a security fix, but its reason names no fork commit"
NOT_CARRIED = "is ported by design as a security fix, but"


@pytest.mark.parametrize(
    ("carried", "cite", "code", "flagged"),
    [
        ("merged", "squash", 0, None),
        ("absent", "squash", 1, NO_CARRIER),
        ("undone", "squash", 1, "`restore.sh:+3`"),
        ("merged", "unrelated", 1, NO_CARRIER),
        ("partial", "squash", 1, "`restore.sh:+3`"),
    ],
)
def test_an_upstream_squash_on_fork_main_carries_its_pre_squash_security_commit(tmp_path, carried, cite, code, flagged):
    config, original = squash_world(tmp_path, carried, cite)
    result = run(config, "--today", "2026-10-27")
    assert result.returncode == code, result.stdout + result.stderr
    if code == 0:
        assert "Upstream parity gate OK" in result.stdout
        assert NOT_CARRIED not in result.stdout
        return
    assert f"`{original[:12]}` {NOT_CARRIED}" in result.stdout
    assert flagged in result.stdout
    if carried in ("partial", "undone"):
        assert "reject_link_members" not in (tmp_path / "fork" / "restore.sh").read_text()


def test_a_squash_that_drops_the_security_line_carries_only_what_it_keeps(tmp_path):
    config, original = squash_world(tmp_path, "partial", "squash")
    result = run(config, "--today", "2026-10-27")
    assert "safety_copy" in (tmp_path / "fork" / "restore.sh").read_text()
    assert result.returncode == 1, result.stdout
    assert "Upstream parity gate OK" not in result.stdout
    assert f"`{original[:12]}` {NOT_CARRIED} 1 of 3 hunk(s) of its own change" in result.stdout
    assert "`restore.sh:+3` is not on refs/remotes/fork/main" in result.stdout
    assert "`restore.sh:+41`" not in result.stdout


@pytest.mark.parametrize(
    ("exception", "message"),
    [
        ("", "`doc.md:+3` is not on refs/remotes/fork/main"),
        (" missing `doc.md:+3` because ;", "names missing `doc.md:+3` without a reason"),
        (
            DOC_EXCEPTION + " missing `restore.sh:+41` because squash <squash> rewrote it;",
            "names missing `restore.sh:+41`, but that hunk is carried to refs/remotes/fork/main",
        ),
        (
            DOC_EXCEPTION + " missing `restore.sh:+7` because squash <squash> moved it;",
            "names missing `restore.sh:+7`, but no hunk",
        ),
    ],
)
def test_every_own_line_a_security_carrier_leaves_out_is_named_with_a_reason(tmp_path, exception, message):
    config, _ = squash_world(tmp_path, "merged", "squash", exception)
    result = run(config, "--today", "2026-10-27")
    assert result.returncode == 1, result.stdout
    assert message in result.stdout


def chain_world(tmp_path, steps, cited, exception="", token_length=40, covered=None):
    upstream = new_repo(tmp_path / "upstream")
    base = commit(upstream, "base", {"a.py": "one\ntwo\n", "b.py": "three\nfour\n"})
    fix = commit(
        upstream,
        "fix: check the token and its scope",
        {"a.py": "one\nCHECK_TOKEN\ntwo\n", "b.py": "three\nCHECK_SCOPE\nfour\n"},
    )
    git(upstream, "tag", "v1.1.0")
    fork = tmp_path / "fork"
    git(tmp_path, "clone", "-q", str(upstream), str(fork))
    for key, value in (("user.name", "Test"), ("user.email", "test@example.com"), ("commit.gpgsign", "false")):
        git(fork, "config", key, value)
    git(fork, "reset", "-q", "--hard", base)
    made = {}
    for step in steps:
        if step == "token":
            made[step] = commit(fork, "port the token check", {"a.py": "one\nCHECK_TOKEN\ntwo\n"})
        elif step == "scope":
            made[step] = commit(fork, "port the scope check", {"b.py": "three\nCHECK_SCOPE\nfour\n"})
        elif step == "drop-scope":
            made[step] = commit(fork, "fork: drop the scope check", {"b.py": "three\nfour\n"})
        elif step == "drop-token":
            made[step] = commit(fork, "fork: drop the token check", {"a.py": "one\ntwo\n"})
        elif step == "trailer":
            made[step] = commit(fork, f"fork: adopt the token fix\n\nUpstream: {fix}", {"f.py": "adopted\n"})
        elif step == "orphan":
            git(fork, "checkout", "-q", "-b", "orphan-side")
            made[step] = commit(
                fork, "side: scope check never merged", {"b.py": "three\nCHECK_SCOPE\nfour\n", "o.py": "side\n"}
            )
            git(fork, "checkout", "-q", "main")
        else:
            made[step] = commit(fork, "fork feature", {"f.py": "fork\n"})
    names = " and ".join(made[step] for step in cited)
    for step, sha in made.items():
        exception = exception.replace(f"<{step}>", sha)
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
    decisions = "".join(
        f"- `{ident}` the owner accepts the fork's own form of {covered or fix} until: 2026-11-10\n"
        for ident in sorted(set(re.findall(r"owner-decision: (2026-10-20-[a-z-]+gateway)", exception)))
    )
    owner = f"\n## Owner decisions\n\n{decisions}" if decisions else ""
    (forks / "kit.md").write_text(
        f"## Ported by design\n\n- `{fix[:token_length]}` ported in fork commits {names};{exception} security: yes, "
        "it checks tokens\n" + owner
    )
    return forks / "forks.toml", fix


SCOPE_DECISION = " missing `b.py:+2` because owner-decision: 2026-10-20-scope-gateway, the fork checks scopes there;"


@pytest.mark.parametrize(
    ("steps", "cited", "exception", "code", "message"),
    [
        (["token", "scope", "feature"], ["token", "scope"], "", 0, None),
        (["token", "scope", "feature"], ["token"], "", 1, "`b.py:+2` is on refs/remotes/fork/main, but no fork commit"),
        (["token", "feature"], ["token"], "", 1, "`b.py:+2` is not on refs/remotes/fork/main"),
        (["token", "feature"], ["token"], SCOPE_DECISION, 0, None),
        (["token", "scope", "drop-scope"], ["token", "scope"], "", 1, "`b.py:+2` is not on refs/remotes/fork/main"),
        (["feature"], ["feature"], "", 1, NO_CARRIER),
    ],
)
def test_a_security_design_port_is_carried_by_a_chain_of_named_fork_commits(
    tmp_path, steps, cited, exception, code, message
):
    config, fix = chain_world(tmp_path, steps, cited, exception)
    result = run(config, "--today", "2026-10-27")
    assert result.returncode == code, result.stdout + result.stderr
    if code == 0:
        assert NOT_CARRIED not in result.stdout
    else:
        assert f"`{fix[:12]}` {NOT_CARRIED}" in result.stdout
        assert message in result.stdout


@pytest.mark.parametrize(
    ("decision", "message"),
    [
        ("2026-10-20-unrecorded", "names owner decision 2026-10-20-unrecorded, which is not recorded"),
        ("2026-10-20-scope-gateway", None),
    ],
    ids=["unrecorded", "recorded"],
)
def test_an_owner_decision_excuses_a_hunk_only_when_it_is_recorded_for_that_commit(tmp_path, decision, message):
    exception = f" missing `b.py:+2` because owner-decision: {decision}, the fork checks scopes there;"
    config, fix = chain_world(tmp_path, ["token", "feature"], ["token"], exception)
    result = run(config, "--today", "2026-10-27")
    if message is None:
        assert result.returncode == 0, result.stdout + result.stderr
        return
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"`{fix}` missing `b.py:+2` {message}" in result.stdout, result.stdout


@pytest.mark.parametrize(
    ("steps", "cited", "exception", "code", "message"),
    [
        (
            ["token", "scope", "orphan"],
            ["token", "scope", "orphan"],
            "",
            1,
            "<orphan>, which is not on refs/remotes/fork/main or on any upstream ref",
        ),
        (
            ["token", "scope", "feature"],
            ["token", "scope", "feature"],
            "",
            1,
            "names fork commit <feature>, which brings none of its hunks",
        ),
        (
            ["token", "scope", "feature"],
            ["token", "scope"],
            " see also 0123456789abcdef0123;",
            1,
            "names `0123456789abcdef0123`, which is not a commit in the fork or upstream history",
        ),
        (
            ["token", "drop-token", "feature"],
            ["token"],
            " missing `a.py:+2` because <drop-token> moved the token check;" + SCOPE_DECISION,
            1,
            "excuses all 2 hunk(s) of its own change; at least one must be carried to refs/remotes/fork/main",
        ),
        (
            ["token", "feature"],
            ["token"],
            " missing `b.py:+2` because the gateway checks scopes;",
            1,
            (
                "names missing `b.py:+2` because the gateway checks scopes, but its reason names no fork commit on "
                "refs/remotes/fork/main that changed b.py, and no owner-decision"
            ),
        ),
        (
            ["token", "feature"],
            ["token"],
            " missing `b.py:+2` because <feature> moved it;",
            1,
            (
                "names missing `b.py:+2` because <feature> moved it, but its reason names no fork commit on "
                "refs/remotes/fork/main that changed b.py, and no owner-decision"
            ),
        ),
        (
            ["trailer"],
            ["trailer"],
            " missing `a.py:+2` because owner-decision: 2026-10-20-token-gateway;" + SCOPE_DECISION,
            1,
            NO_CARRIER,
        ),
        (
            ["token", "scope", "drop-scope"],
            ["token", "scope"],
            " missing `b.py:+2` because <drop-scope> moved the scope check to the gateway;",
            0,
            None,
        ),
        (["token", "feature"], ["token"], SCOPE_DECISION, 0, None),
    ],
    ids=[
        "h1-off-main",
        "h2-brings-nothing",
        "not-a-commit",
        "h4-all-excused",
        "no-sha",
        "sha-off-path",
        "trailer-only",
        "breaking-commit",
        "owner-decision",
    ],
)
def test_every_commit_and_exception_of_a_security_design_port_is_accounted_for(
    tmp_path, steps, cited, exception, code, message
):
    config, fix = chain_world(tmp_path, steps, cited, exception)
    result = run(config, "--today", "2026-10-27")
    assert result.returncode == code, result.stdout + result.stderr
    if code == 0:
        assert "Upstream parity gate OK" in result.stdout
        assert NOT_CARRIED not in result.stdout
        return
    made = dict(
        line.split(" ", 1)[::-1] for line in git(tmp_path / "fork", "log", "--all", "--format=%H %s").splitlines()
    )
    shas = {
        "orphan": made["side: scope check never merged"] if "orphan" in steps else None,
        "feature": made.get("fork feature"),
        "drop-token": made.get("fork: drop the token check"),
    }
    for key, sha in shas.items():
        if sha:
            message = message.replace(f"<{key}>", sha)
    assert f"`{fix[:12]}` {NOT_CARRIED}" in result.stdout
    assert message in result.stdout, result.stdout


@pytest.mark.parametrize(
    ("exception", "code", "message"),
    [
        (DOC_EXCEPTION + " missing `restore.sh:+3` because x;", 1, "names missing `restore.sh:+3` because x, but"),
        (
            DOC_EXCEPTION + " missing `restore.sh:+3` because <unrelated> reworked it;",
            1,
            (
                "names missing `restore.sh:+3` because <unrelated> reworked it, but its reason names no fork commit "
                "on refs/remotes/fork/main that changed restore.sh, and no owner-decision"
            ),
        ),
        (DOC_EXCEPTION + " missing `restore.sh:+3` because squash <squash> dropped it;", 0, None),
    ],
    ids=["h5-one-letter", "h5-sha-off-path", "h5-names-the-squash"],
)
def test_a_squash_that_drops_the_security_line_needs_the_commit_that_dropped_it(tmp_path, exception, code, message):
    config, original = squash_world(tmp_path, "partial", "squash", exception)
    result = run(config, "--today", "2026-10-27")
    assert "reject_link_members" not in (tmp_path / "fork" / "restore.sh").read_text()
    assert result.returncode == code, result.stdout + result.stderr
    if code == 0:
        assert "Upstream parity gate OK" in result.stdout
        return
    unrelated = git(tmp_path / "upstream", "log", "-1", "--format=%H", "--grep", "chore: tidy", "main")
    assert f"`{original[:12]}` {NOT_CARRIED}" in result.stdout
    assert message.replace("<unrelated>", unrelated) in result.stdout, result.stdout


def comment_world(tmp_path, upstream_text, fork_text, name="a.go"):
    upstream = new_repo(tmp_path / "upstream")
    base = commit(upstream, "base", {name: "one\ntwo\n"})
    fix = commit(upstream, "fix: check the token", {name: upstream_text})
    git(upstream, "tag", "v1.1.0")
    fork = tmp_path / "fork"
    git(tmp_path, "clone", "-q", str(upstream), str(fork))
    for key, value in (("user.name", "Test"), ("user.email", "test@example.com"), ("commit.gpgsign", "false")):
        git(fork, "config", key, value)
    git(fork, "reset", "-q", "--hard", base)
    port = commit(fork, "port the token check without its comments", {name: fork_text})
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
        f"## Ported by design\n\n- `{fix}` ported in fork commit {port}; security: yes, it checks tokens\n"
    )
    return forks / "forks.toml", fix


@pytest.mark.parametrize(
    ("name", "upstream_text", "fork_text", "code"),
    [
        ("a.go", "one\n// explain the check\nCHECK_TOKEN\n\n/* and why */\ntwo\n", "one\nCHECK_TOKEN\ntwo\n", 0),
        ("a.py", "one\n# explain the check\nCHECK_TOKEN\ntwo\n", "one\nCHECK_TOKEN\ntwo\n", 0),
        ("a.go", "one\n// explain the check\nCHECK_TOKEN\nCHECK_SCOPE\ntwo\n", "one\nCHECK_TOKEN\ntwo\n", 1),
        ("a.py", "one\n# explain the check\nCHECK_TOKEN\nCHECK_SCOPE\ntwo\n", "one\nCHECK_SCOPE\ntwo\n", 1),
        ("a.md", "one\n# Token check\nCHECK_TOKEN\ntwo\n", "one\nCHECK_TOKEN\ntwo\n", 1),
    ],
    ids=["go-comments-only", "py-comments-only", "go-code-missing", "py-code-missing", "md-heading-is-content"],
)
def test_a_hunk_missing_only_comment_lines_is_carried_but_never_a_missing_code_line(
    tmp_path, name, upstream_text, fork_text, code
):
    config, fix = comment_world(tmp_path, upstream_text, fork_text, name)
    result = run(config, "--today", "2026-10-27")
    assert result.returncode == code, result.stdout + result.stderr
    if code == 0:
        assert "Upstream parity gate OK" in result.stdout
        assert NOT_CARRIED not in result.stdout
    else:
        assert f"`{fix[:12]}` {NOT_CARRIED}" in result.stdout, result.stdout


@pytest.mark.parametrize("token_length", [40, 12], ids=["full-token", "short-token"])
def test_an_owner_decision_for_another_commit_never_excuses_a_security_hunk(tmp_path, token_length):
    other = "0" * 39 + "1"
    config, fix = chain_world(tmp_path, ["token", "feature"], ["token"], SCOPE_DECISION, token_length, other)
    result = run(config, "--today", "2026-10-27")
    assert result.returncode == 1, result.stdout + result.stderr
    assert fix[:12] in result.stdout
    assert "is not covered by owner decision 2026-10-20-scope-gateway" in result.stdout, result.stdout
