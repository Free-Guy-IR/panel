import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "upstream_inventory.py"
ENV = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")} | {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def lines(prefix, count=10, overrides=None, extra=()):
    body = [f"{prefix}{n}" for n in range(1, count + 1)]
    for index, text in (overrides or {}).items():
        body[index - 1] = text
    return "\n".join([*body, *extra]) + "\n"


def lock(version):
    return f'version = 1\n\n[[package]]\nname = "pasarguard-node-bridge"\nversion = "{version}"\n'


def pyproject(floor):
    return f'[project]\nname = "panel"\ndependencies = [\n    "pasarguard-node-bridge>={floor}",\n]\n'


def git(repo, *args):
    result = subprocess.run(["git", *args], cwd=repo, env=ENV, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def commit(repo, message, files):
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(repo, "add", *files)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def run(cwd, *args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], cwd=cwd, env=ENV, capture_output=True, text=True, check=False
    )


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    root = tmp_path_factory.mktemp("upstream-inventory")
    path = root / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "up")
    for key, value in (
        ("user.name", "Test"),
        ("user.email", "test@example.invalid"),
        ("commit.gpgsign", "false"),
        ("tag.gpgsign", "false"),
    ):
        git(path, "config", key, value)

    shas = {}
    shas["base"] = commit(
        path,
        "upstream base",
        {
            "a.py": lines("a"),
            "b.py": lines("b"),
            "b2.py": lines("x"),
            "c.py": lines("c"),
            "d.py": lines("d"),
            "pyproject.toml": pyproject("0.9.1"),
            "uv.lock": lock("0.9.1"),
        },
    )
    git(path, "tag", "v1.0")
    shas["u1"] = commit(path, "upstream changes a", {"a.py": lines("a", overrides={3: "A3"})})
    shas["u2"] = commit(
        path,
        "upstream changes b and b2",
        {"b.py": lines("b", overrides={2: "B2"}), "b2.py": lines("x", overrides={2: "X2"})},
    )
    shas["u3"] = commit(path, "upstream changes c", {"c.py": lines("c", overrides={5: "C5"})})
    git(path, "tag", "-a", "v1.1", "-m", "v1.1")
    shas["u4"] = commit(
        path,
        "upstream adds files",
        {"new.py": lines("n", 5), "new2.py": lines("m", 5), "new3.py": lines("o", 5)},
    )
    shas["u5"] = commit(path, "upstream raises the bridge floor", {"pyproject.toml": pyproject("0.9.2")})

    git(path, "checkout", "-q", "-b", "feat-x", shas["base"])
    shas["f1"] = commit(path, "feature one", {"feature.py": lines("f", 3)})
    git(path, "cherry-pick", shas["u1"])
    shas["dup"] = git(path, "rev-parse", "HEAD")
    shas["f2"] = commit(path, "feature two", {"feature.py": lines("f", 4)})

    git(path, "checkout", "-q", "-b", "feat-y", shas["base"])
    git(path, "cherry-pick", shas["u3"])
    shas["g"] = git(path, "rev-parse", "HEAD")

    for ref, sha in (("main", "base"), ("dev", "u5"), ("feat/x", "f2"), ("feat/y", "g")):
        git(path, "update-ref", f"refs/remotes/upstream/{ref}", git(path, "rev-parse", shas[sha]))
    git(path, "symbolic-ref", "refs/remotes/upstream/HEAD", "refs/remotes/upstream/main")

    git(path, "checkout", "-q", "--orphan", "main", shas["base"])
    git(path, "commit", "-q", "-m", "fork root")
    git(path, "tag", "v1.0-fork")
    git(path, "branch", "-q", "-D", "up", "feat-x", "feat-y")
    commit(
        path,
        "fork changes",
        {
            "uv.lock": lock("0.9.1+singbox"),
            "fork_only.py": lines("k", 3),
            "d.py": lines("d", overrides={4: "D4-fork"}),
            "c.py": lines("c", extra=["fork c line"]),
        },
    )
    git(path, "cherry-pick", "-x", shas["u1"])
    commit(path, "fork extends a", {"a.py": lines("a", overrides={3: "A3"}, extra=["fork a line"])})
    git(path, "cherry-pick", "-x", shas["u2"])
    commit(
        path,
        "fork overrides b and b2",
        {
            "b.py": lines("b", overrides={2: "B2", 5: "FORK5"}),
            "b2.py": lines("x", overrides={2: "FORK2"}),
        },
    )
    git(path, "cherry-pick", "-x", shas["u4"])
    commit(
        path,
        "fork changes ported files",
        {"new2.py": lines("m", 5, overrides={3: "FORK3"}), "new3.py": lines("o", 5, extra=["fork o line"])},
    )

    git(path, "checkout", "-q", "-b", "waiver-ok")
    commit(
        path,
        "waive bridge",
        {"upstream-bridge-waiver.toml": 'upstream_version = "0.9.2"\nreason = "fork bridge carries sing-box"\n'},
    )
    git(path, "checkout", "-q", "-b", "waiver-noreason", "main")
    commit(path, "waive bridge badly", {"upstream-bridge-waiver.toml": 'upstream_version = "0.9.2"\nreason = ""\n'})
    git(path, "checkout", "-q", "main")

    (path / ".upstream-sync-point").write_text(shas["u5"] + "\n")
    plain = root / "no-sync-point"
    git(path, "worktree", "add", "-q", "--detach", str(plain), "main")
    return {"path": path, "plain": plain, "shas": shas}


def inventory(cwd, out, *extra):
    result = run(cwd, "--base", "v1.0", "--head", "main", "--json-file", str(out), *extra)
    assert result.returncode == 0, result.stderr
    report = json.loads(out.read_text())
    return report, {f["path"]: f for f in report["files"]}


def ledger(tmp_path, text):
    path = tmp_path / "UPSTREAM_SYNC.md"
    path.write_text("# ledger\n\n    - `0000000000` example in the intro is ignored\n\n" + text)
    return str(path)


def parity(repo, ledger_path, *extra):
    return run(
        repo["path"],
        "--base",
        "v1.0",
        "--head",
        "main",
        "--track",
        "refs/remotes/upstream/*",
        "--track",
        "v1.*",
        "--fail-on-unported",
        ledger_path,
        *extra,
    )


def tally(stdout, label):
    match = re.search(rf"^  {re.escape(label)}\s+(\d+)$", stdout, re.MULTILINE)
    assert match, f"{label!r} missing from:\n{stdout}"
    return int(match.group(1))


def unported_block(stdout):
    return stdout.split("Unported upstream commits", 1)[1].split("\n\n", 1)[0] if "Unported upstream" in stdout else ""


def test_dual_base_classification(repo, tmp_path):
    report, files = inventory(repo["path"], tmp_path / "r.json")
    assert report["upstream_head"]["sha"] == repo["shas"]["u5"]
    assert files["a.py"]["category"] == "pure-addition"
    assert files["c.py"]["category"] == "pure-addition"
    assert files["new3.py"]["category"] == "pure-addition"
    assert "new.py" not in files
    assert files["fork_only.py"]["category"] == "fork-only"
    assert files["uv.lock"]["category"] == "mechanical"
    assert files["b.py"]["category"] == "override"
    assert files["b.py"]["removed_lines"] == 1
    assert [(r["start"], r["end"]) for r in files["b.py"]["override_ranges"]] == [(5, 5)]
    assert files["b2.py"]["category"] == "override"
    assert files["b2.py"]["removed_lines"] == 1
    assert files["d.py"]["category"] == "override"
    assert files["new2.py"]["category"] == "override"
    assert files["new2.py"]["status"] == "added"
    assert report["summary"]["override"] == 4
    assert report["summary"]["override_lines"] == 4


def test_without_upstream_head_matches_single_base(repo, tmp_path):
    none_report, none_files = inventory(repo["path"], tmp_path / "none.json", "--upstream-head", "none")
    plain_report, _ = inventory(repo["plain"], tmp_path / "plain.json")
    assert "upstream_head" not in none_report
    assert none_report["files_with_hashes"] == plain_report["files_with_hashes"]
    assert none_report["summary"] == plain_report["summary"]
    assert none_files["a.py"]["category"] == "override"
    assert none_files["new.py"]["category"] == "fork-only"
    assert none_files["new2.py"]["category"] == "fork-only"
    assert none_files["b.py"]["removed_lines"] == 2


def test_override_ledger_gate_uses_dual_base(repo, tmp_path):
    overrides = tmp_path / "OVERRIDES.md"
    overrides.write_text("- `b.py` x\n- `b2.py` x\n- `d.py` x\n- `new2.py` x\n")
    ok = run(repo["path"], "--base", "v1.0", "--head", "main", "--fail-on-override-outside-ledger", str(overrides))
    assert ok.returncode == 0, ok.stdout + ok.stderr
    overrides.write_text("- `b.py` x\n- `b2.py` x\n- `d.py` x\n")
    missing = run(repo["path"], "--base", "v1.0", "--head", "main", "--fail-on-override-outside-ledger", str(overrides))
    assert missing.returncode == 1
    assert "  new2.py" in missing.stdout


def test_unported_gate_lists_exactly_the_uncovered_commits(repo, tmp_path):
    shas = repo["shas"]
    result = parity(repo, ledger(tmp_path, "## Ported by design\n\n## Waived\n"))
    assert result.returncode == 1, result.stderr
    block = unported_block(result.stdout)
    listed = {sha for sha in shas.values() if sha[:12] in block}
    assert listed == {shas["u3"], shas["u5"], shas["f1"], shas["f2"], shas["g"]}
    assert f"on upstream/dev, v1.1 (1):\n    {shas['u3'][:12]}" in block
    assert f"on upstream/dev (1):\n    {shas['u5'][:12]}" in block
    assert "on upstream/feat/x (2):" in block
    assert "upstream/HEAD" not in result.stdout
    assert "warning: skipped v1.0-fork" in result.stdout
    assert tally(result.stdout, "ported via cherry-pick -x trailer") == 3
    assert tally(result.stdout, "patch-id equivalent of a covered commit") == 1
    assert tally(result.stdout, "unported") == 5
    assert "Upstream parity gate FAILED" in result.stdout


def test_ledger_entries_and_patch_id_copies_cover(repo, tmp_path):
    shas = repo["shas"]
    text = (
        "## Ported by design\n\n"
        f"- `{shas['f1']}` rewritten as fork code\n"
        f"- `{shas['f2'][:12]}` — folded into the fork feature\n\n"
        "## Waived\n\n"
        f"- `{shas['u3']}` the fork keeps c as it is\n"
        f"- `{shas['u5'][:10]}` the fork bridge has its own floor\n"
        f"- `{shas['base']}` older than the base\n"
    )
    result = parity(repo, ledger(tmp_path, text))
    assert result.returncode == 0, result.stdout + result.stderr
    assert tally(result.stdout, "ported via cherry-pick -x trailer") == 3
    assert tally(result.stdout, "ported by design (ledger)") == 2
    assert tally(result.stdout, "waived (ledger)") == 2
    assert tally(result.stdout, "patch-id equivalent of a covered commit") == 2
    assert tally(result.stdout, "unported") == 0
    assert "Ledger warnings (1):" in result.stdout
    assert shas["base"] in result.stdout
    assert "Upstream parity gate OK" in result.stdout


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ("- `{u3}`\n", "has no reason"),
        ("- `{u3}` — \n", "has no reason"),
        ("- `deadbeef00` a reason\n", "matches no upstream commit"),
        ("- `{short}` a reason\n", "at least 9 hex characters"),
        ("- {u3} no backticks\n", "is not in the form"),
    ],
)
def test_ledger_errors_fail_the_gate(repo, tmp_path, entry, message):
    shas = repo["shas"]
    body = entry.format(u3=shas["u3"], short=shas["u3"][:7])
    waived_rest = "".join(f"- `{shas[k]}` reason\n" for k in ("u5", "f1", "f2"))
    result = parity(repo, ledger(tmp_path, f"## Waived\n\n{waived_rest}{body}"))
    assert result.returncode == 1
    assert message in result.stdout
    assert "ledger error" in result.stdout


def test_track_usage_errors(repo, tmp_path):
    empty = ledger(tmp_path, "")
    nothing = run(repo["path"], "--base", "v1.0", "--track", "refs/remotes/nowhere/*", "--fail-on-unported", empty)
    assert nothing.returncode == 2
    assert "matched no commit refs" in nothing.stderr
    lonely = run(repo["path"], "--base", "v1.0", "--track", "refs/remotes/upstream/*")
    assert lonely.returncode == 2


@pytest.mark.parametrize(
    ("head", "args", "code", "message"),
    [
        ("main", ["--bridge-latest", "0.9.1", "--upstream-head", "u4"], 0, "Bridge version gate OK"),
        ("main", ["--bridge-latest", "0.9.2", "--upstream-head", "u4"], 1, "0.9.2 (from --bridge-latest)"),
        ("main", ["--bridge-latest", "0.9.1"], 1, "upstream latest: 0.9.2 (from pyproject.toml on"),
        ("waiver-ok", ["--bridge-latest", "0.9.1"], 0, "waived by upstream-bridge-waiver.toml for 0.9.2"),
        ("waiver-ok", ["--bridge-latest", "0.9.3"], 1, "is stale: it waives 0.9.2 but upstream is now at 0.9.3"),
        ("waiver-noreason", ["--bridge-latest", "0.9.2", "--upstream-head", "none"], 1, "has no reason"),
        ("main", ["--bridge-latest", "not-a-version"], 2, "is not a version"),
    ],
)
def test_bridge_gate(repo, head, args, code, message):
    args = [repo["shas"].get(a, a) for a in args]
    result = run(repo["path"], "--head", head, "--check-bridge", *args)
    assert result.returncode == code, result.stdout + result.stderr
    assert message in result.stdout
    assert "pinned: 0.9.1+singbox (compared as 0.9.1)" in result.stdout
    assert "Upstream Inventory" not in result.stdout


def test_checks_compose_to_the_worst_exit_code(repo, tmp_path):
    empty = ledger(tmp_path, "## Waived\n")
    passing_bridge = ["--check-bridge", "--bridge-latest", "0.9.1", "--upstream-head", repo["shas"]["u4"]]
    combined = parity(repo, empty, *passing_bridge)
    assert combined.returncode == 1
    assert "Upstream parity gate FAILED" in combined.stdout
    assert "Bridge version gate OK" in combined.stdout
    broken_bridge = ["--check-bridge", "--bridge-latest", "not-a-version"]
    worst = parity(repo, empty, *broken_bridge)
    assert worst.returncode == 2
    assert "Upstream parity gate FAILED" in worst.stdout
