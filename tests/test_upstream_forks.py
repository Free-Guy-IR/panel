import json
import os
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
        f"- `{shas['bumped']}` the fork versions itself\n"
        f"- `{shas['feature']}` pending: tracked feature branch until: 2026-10-25\n"
    )
    config = write_map(world, ledger_map(world), ledger)
    assert run(config, "--today", "2026-10-01").returncode == 0
    expired = run(config, "--today", "2026-10-26")
    assert expired.returncode == 1
    assert "was decided only until 2026-10-25" in expired.stdout


def test_the_refs_snapshot_of_a_ledger_fork_is_written_and_enforced(world):
    shas = world["shas"]
    ledger = "## Waived\n\n" + "".join(f"- `{shas[k]}` reason\n" for k in ("bumped", "feature"))
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
