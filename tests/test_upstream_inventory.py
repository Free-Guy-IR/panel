import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "upstream_inventory.py"
ADAPTED_PICKS = {}
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


def dashboard_package(xray_spec):
    return json.dumps(
        {"name": "dash", "dependencies": {"@pasarguard/core-kit": "0.2.9", "@pasarguard/xray-config-kit": xray_spec}}
    )


def bun_lock(top, nested):
    return (
        "{\n"
        '  "lockfileVersion": 1,\n'
        '  "workspaces": {\n'
        '    "": {\n'
        '      "name": "dash",\n'
        '      "dependencies": {\n'
        '        "@pasarguard/core-kit": "file:../../core-kit",\n'
        '        "@pasarguard/xray-config-kit": "^0.3.8",\n'
        "      },\n"
        "    },\n"
        "  },\n"
        '  "packages": {\n'
        '    "@pasarguard/core-kit": ["@pasarguard/core-kit@file:../../core-kit", { "dependencies": {} }],\n'
        f'    "@pasarguard/core-kit/@pasarguard/xray-config-kit": ["@pasarguard/xray-config-kit@{nested}", "", {{}}, "sha512-n"],\n'
        f'    "@pasarguard/xray-config-kit": ["@pasarguard/xray-config-kit@{top}", "", {{}}, "sha512-t"],\n'
        '    "zod": ["zod@3.23.8", "", {}, "sha512-z"],\n'
        "  }\n"
        "}\n"
    )


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
        ("user.email", "test@example.com"),
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
            "dashboard/package.json": dashboard_package("^0.3.8"),
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

    git(path, "checkout", "-q", "-b", "rel-merged", shas["u5"])
    git(path, "merge", "-q", "--no-ff", "-m", "merge feat/x", "feat-x")
    git(path, "checkout", "-q", "-b", "rel-rebased", shas["u5"])
    git(path, "cherry-pick", shas["f1"])
    shas["f1_copy"] = git(path, "rev-parse", "HEAD")
    git(path, "checkout", "-q", "-b", "rel-empty", shas["u5"])
    git(path, "commit", "-q", "--allow-empty", "-m", "upstream empty commit")
    shas["empty"] = git(path, "rev-parse", "HEAD")
    git(path, "checkout", "-q", "-b", "rel-npm-floor", shas["u5"])
    commit(path, "upstream raises the xray kit floor", {"dashboard/package.json": dashboard_package("^0.3.9")})
    git(path, "checkout", "-q", "-b", "rel-npm-lock", shas["u5"])
    commit(path, "upstream locks the newer xray kit", {"dashboard/bun.lock": bun_lock("0.3.8", "0.3.9")})
    for name in ("merged", "rebased", "empty", "npm-floor", "npm-lock"):
        git(path, "update-ref", f"refs/upstream-release/{name}", git(path, "rev-parse", f"rel-{name}"))

    for ref, sha in (("main", "base"), ("dev", "u5"), ("feat/x", "f2"), ("feat/y", "g")):
        git(path, "update-ref", f"refs/remotes/upstream/{ref}", git(path, "rev-parse", shas[sha]))
    git(path, "symbolic-ref", "refs/remotes/upstream/HEAD", "refs/remotes/upstream/main")

    git(path, "checkout", "-q", "--orphan", "main", shas["base"])
    git(path, "commit", "-q", "-m", "fork root")
    git(path, "tag", "v1.0-fork")
    git(
        path,
        "branch",
        "-q",
        "-D",
        "up",
        "feat-x",
        "feat-y",
        "rel-merged",
        "rel-rebased",
        "rel-empty",
        "rel-npm-floor",
        "rel-npm-lock",
    )
    commit(
        path,
        "fork changes",
        {
            "uv.lock": lock("0.9.1+singbox"),
            "fork_only.py": lines("k", 3),
            "d.py": lines("d", overrides={4: "D4-fork"}),
            "c.py": lines("c", extra=["fork c line"]),
            "dashboard/bun.lock": bun_lock("0.3.8", "0.3.8"),
        },
    )
    git(path, "cherry-pick", "-x", shas["u1"])
    shas["u1_pick"] = git(path, "rev-parse", "HEAD")
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
    git(path, "checkout", "-q", "-b", "npm-waiver-ok", "main")
    commit(
        path,
        "waive xray kit",
        {
            "upstream-npm-waivers.toml": (
                '[[waiver]]\npackage = "@pasarguard/xray-config-kit"\nupstream_version = "0.3.9"\n'
                'reason = "0.3.9 only changes docs"\n'
            )
        },
    )
    git(path, "checkout", "-q", "-b", "npm-waiver-dated", "main")
    commit(
        path,
        "waive xray kit for a while",
        {
            "upstream-npm-waivers.toml": (
                '[[waiver]]\npackage = "@pasarguard/xray-config-kit"\nupstream_version = "0.3.9"\n'
                'reason = "nodes still run an older Xray-core"\nuntil = 2026-10-25\n'
            )
        },
    )
    git(path, "checkout", "-q", "-b", "npm-nested-behind", "main")
    commit(path, "core-kit pins an older xray kit", {"dashboard/bun.lock": bun_lock("0.3.8", "0.3.7")})
    git(path, "checkout", "-q", "-b", "reverted-pick", "main")
    git(path, "revert", "--no-edit", shas["u1_pick"])
    git(path, "checkout", "-q", "main")
    ADAPTED_PICKS.update(
        {
            shas["u2"]: "the fork replaces upstream's X2 in b2.py with its own line",
            shas["u4"]: "the fork replaces upstream's m3 in new2.py with its own line",
        }
    )

    (path / ".upstream-sync-point").write_text(shas["u5"] + "\n")
    plain = root / "no-sync-point"
    git(path, "worktree", "add", "-q", "--detach", str(plain), "main")
    return {"path": path, "plain": plain, "shas": shas}


def inventory(cwd, out, *extra):
    result = run(cwd, "--base", "v1.0", "--head", "main", "--json-file", str(out), *extra)
    assert result.returncode == 0, result.stderr
    report = json.loads(out.read_text())
    return report, {f["path"]: f for f in report["files"]}


def ledger(tmp_path, text, adapted=True):
    path = tmp_path / "UPSTREAM_SYNC.md"
    extra = ""
    if adapted:
        picks = "".join(f"- `{sha}` {why}\n" for sha, why in ADAPTED_PICKS.items())
        if "## Ported by design\n\n" in text:
            text = text.replace("## Ported by design\n\n", "## Ported by design\n\n" + picks, 1)
        else:
            extra = "\n## Ported by design\n\n" + picks
    path.write_text("# ledger\n\n    - `0000000000` example in the intro is ignored\n\n" + text + extra)
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
        "--today",
        "2026-10-06",
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
    assert tally(result.stdout, "ported via cherry-pick -x trailer") == 1
    assert tally(result.stdout, "ported by design (ledger)") == 2
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
    assert tally(result.stdout, "ported via cherry-pick -x trailer") == 1
    assert tally(result.stdout, "ported by design (ledger)") == 4
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


def test_an_old_tag_whose_commits_are_already_in_the_base_is_covered(tmp_path):
    path = tmp_path / "old-tag"
    path.mkdir()
    git(path, "init", "-q", "-b", "up")
    for key, value in (("user.name", "Test"), ("user.email", "test@example.com"), ("commit.gpgsign", "false")):
        git(path, "config", key, value)
    root = commit(path, "root", {"r.py": lines("r")})
    git(path, "checkout", "-q", "-b", "old", root)
    old_fix = commit(path, "old release fix", {"o.py": lines("o", 3)})
    git(path, "tag", "v0.9")
    git(path, "checkout", "-q", "up")
    commit(path, "upstream work", {"u.py": lines("u", 2)})
    git(path, "cherry-pick", old_fix)
    commit(path, "upstream base", {"r.py": lines("r", overrides={1: "R1"})})
    git(path, "tag", "v1.0")
    git(path, "checkout", "-q", "--orphan", "main", "v1.0")
    git(path, "commit", "-q", "-m", "fork root")
    (path / "UPSTREAM_SYNC.md").write_text("## Ported by design\n\n## Waived\n")

    result = run(path, "--base", "v1.0", "--head", "main", "--track", "v0.9", "--fail-on-unported", "UPSTREAM_SYNC.md")

    assert result.returncode == 0, result.stdout + result.stderr
    assert tally(result.stdout, "patch-id equivalent already in the base") == 1
    assert tally(result.stdout, "unported") == 0


@pytest.mark.parametrize(
    ("today", "code", "message"),
    [
        ("2026-10-24", 0, "Upstream parity gate OK"),
        ("2026-10-25", 0, "Upstream parity gate OK"),
        ("2026-10-26", 1, "was decided only until 2026-10-25"),
    ],
)
def test_a_dated_decision_covers_only_until_its_date(repo, tmp_path, today, code, message):
    shas = repo["shas"]
    text = f"## Waived\n\n- `{shas['f1']}` pending: tracked feature branch, re-check until: 2026-10-25\n" + "".join(
        f"- `{shas[k]}` reason\n" for k in ("u3", "u5", "f2")
    )
    result = parity(repo, ledger(tmp_path, text), "--today", today)
    assert result.returncode == code, result.stdout + result.stderr
    assert message in result.stdout


OWED_LABEL = "owed: in the port queue until its deadline"


def queue_ledger(tmp_path, repo, queue, waived=("f1", "f2")):
    shas = repo["shas"]

    def line(key, reason):
        if "security:" not in reason:
            reason = f"{reason} security: no"
        return f"- `{shas[key]}` {reason}\n"

    return ledger(
        tmp_path,
        "## Waived\n\n"
        + "".join(f"- `{shas[key]}` the fork does not take feature x\n" for key in waived)
        + "\n## Port queue\n\n"
        + "".join(line(key, reason) for key, reason in queue),
    )


@pytest.mark.parametrize(
    ("today", "code", "message"),
    [
        ("2026-10-06", 0, "Upstream parity gate OK, NOT at parity"),
        ("2026-10-13", 0, "the gate fails on 2026-10-14 unless they are ported or re-decided"),
        ("2026-10-14", 1, "`{u5}` was decided only until 2026-10-13"),
    ],
)
def test_a_queued_port_is_owed_and_covers_only_until_its_deadline(repo, tmp_path, today, code, message):
    shas = repo["shas"]
    queue = [("u3", "security fix, port after B1 until: 2026-10-20"), ("u5", "bridge floor, port until: 2026-10-13")]
    result = parity(repo, queue_ledger(tmp_path, repo, queue), "--today", today)
    assert result.returncode == code, result.stdout + result.stderr
    assert message.format(u5=shas["u5"]) in result.stdout
    if code == 0:
        assert tally(result.stdout, OWED_LABEL) == 3
        assert tally(result.stdout, "patch-id equivalent of a covered commit") == 1
        owed = result.stdout.split("Owed upstream ports (3)", 1)[1].split("\n\n", 1)[0]
        assert f"{shas['u5'][:12]}  until 2026-10-13" in owed
        assert f"{shas['u3'][:12]}  until 2026-10-20" in owed
        assert f"{shas['g'][:12]}  until 2026-10-20" in owed
        assert "Upstream parity gate OK: all" not in result.stdout
    else:
        assert shas["u5"][:12] in unported_block(result.stdout)


@pytest.mark.parametrize(
    ("reason", "message"),
    [
        ("port it later", "is in the port queue without an until: YYYY-MM-DD deadline"),
        ("port it later until: 2026-11-06", "more than 30 days ahead; a dated decision needs a nearer deadline"),
        ("port it later until: 2026-13-01", "has an invalid until date 2026-13-01"),
    ],
)
def test_a_queued_port_needs_a_near_deadline(repo, tmp_path, reason, message):
    queue = [("u3", reason), ("u5", "port until: 2026-10-13")]
    result = parity(repo, queue_ledger(tmp_path, repo, queue), "--today", "2026-10-06")
    assert result.returncode == 1, result.stdout + result.stderr
    assert message in result.stdout
    assert repo["shas"]["u3"][:12] in unported_block(result.stdout)


def test_a_section_name_counts_at_any_heading_level(repo, tmp_path):
    shas = repo["shas"]
    text = (
        "## Waived\n\n"
        f"- `{shas['f1']}` the fork does not take feature x\n\n"
        "### Feature x follow-ups\n\n"
        f"- `{shas['f2']}` still waived inside a subsection\n\n"
        "### Port queue\n\n"
        f"- `{shas['u3']}` port it security: no until: 2026-11-30\n"
        f"- `{shas['u5']}` port it security: no until: 2026-10-13\n"
    )
    result = parity(repo, ledger(tmp_path, text), "--today", "2026-10-06")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "more than 30 days ahead; a dated decision needs a nearer deadline" in result.stdout
    assert tally(result.stdout, "waived (ledger)") == 2
    assert tally(result.stdout, OWED_LABEL) == 1


def test_an_unknown_heading_at_the_same_level_closes_a_section(repo, tmp_path):
    shas = repo["shas"]
    text = (
        "### Waived\n\n"
        f"- `{shas['f1']}` the fork does not take feature x\n\n"
        "### Release notes\n\n"
        f"- `{shas['f2']}` not a decision, only a note\n"
        f"- `{shas['u3']}` not a decision either\n"
        f"- `{shas['u5']}` nor this\n"
    )
    result = parity(repo, ledger(tmp_path, text, adapted=False), "--today", "2026-10-06")
    assert result.returncode == 1, result.stdout + result.stderr
    assert tally(result.stdout, "waived (ledger)") == 1
    assert shas["f2"][:12] in unported_block(result.stdout)


def mixed_origin_repo(tmp_path):
    path = tmp_path / "mixed"
    path.mkdir()
    git(path, "init", "-q", "-b", "up")
    for key, value in (("user.name", "Test"), ("user.email", "test@example.com"), ("commit.gpgsign", "false")):
        git(path, "config", key, value)
    commit(path, "root", {"r.py": lines("r")})
    git(path, "tag", "v1.0")
    git(path, "checkout", "-q", "-b", "feat-a", "v1.0")
    shas = {"a": commit(path, "feature", {"x.py": lines("x", 3)})}
    for name in ("feat-b", "rel"):
        git(path, "checkout", "-q", "-b", name, "v1.0")
        git(path, "cherry-pick", shas["a"])
        git(path, "commit", "-q", "--amend", "-m", f"feature copy on {name}")
        shas[name] = git(path, "rev-parse", "HEAD")
    for name in ("feat-a", "feat-b", "rel"):
        git(path, "update-ref", f"refs/remotes/upstream/{name}", git(path, "rev-parse", name))
    git(path, "checkout", "-q", "--orphan", "main", "v1.0")
    git(path, "commit", "-q", "-m", "fork root")
    (path / "UPSTREAM_SYNC.md").write_text(
        "## Waived\n\n"
        f"- `{shas['a']}` UNMERGED-WIP: only on upstream feat-a\n\n"
        "## Port queue\n\n"
        f"- `{shas['feat-b']}` port it security: no until: 2026-10-13\n"
    )
    return path, shas


@pytest.mark.parametrize(("today", "code"), [("2026-10-06", 0), ("2026-10-14", 1)])
def test_a_release_copy_of_a_waived_and_a_queued_commit_is_owed(tmp_path, today, code):
    path, shas = mixed_origin_repo(tmp_path)
    result = run(
        path,
        "--base",
        "v1.0",
        "--head",
        "main",
        "--track",
        "refs/remotes/upstream/*",
        "--fail-on-unported",
        "UPSTREAM_SYNC.md",
        "--release-ref",
        "refs/remotes/upstream/rel",
        "--today",
        today,
    )
    assert result.returncode == code, result.stdout + result.stderr
    if code == 0:
        assert tally(result.stdout, OWED_LABEL) == 2
        assert f"{shas['rel'][:12]}  until 2026-10-13" in result.stdout
        assert tally(result.stdout, "patch-id equivalent of a covered commit") == 0
    else:
        assert shas["rel"][:12] in unported_block(result.stdout)


def test_every_dated_decision_needs_a_near_deadline(repo, tmp_path):
    shas = repo["shas"]
    text = "## Waived\n\n" + "".join(f"- `{shas[k]}` reason\n" for k in ("u3", "u5", "f2"))
    text += f"- `{shas['f1']}` UNMERGED-WIP: only on upstream feat/x until: 2026-11-30\n"
    result = parity(repo, ledger(tmp_path, text), "--today", "2026-10-06")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "is decided until 2026-11-30, more than 30 days ahead" in result.stdout
    assert shas["f1"][:12] in unported_block(result.stdout)


def test_owed_ports_are_annotated_on_github_actions(repo, tmp_path):
    queue = [("u3", "port until: 2026-10-20"), ("u5", "port until: 2026-10-13")]
    args = [
        sys.executable,
        str(SCRIPT),
        "--base",
        "v1.0",
        "--head",
        "main",
        "--track",
        "refs/remotes/upstream/*",
        "--track",
        "v1.*",
        "--fail-on-unported",
        queue_ledger(tmp_path, repo, queue),
        "--today",
        "2026-10-06",
    ]
    plain = subprocess.run(args, cwd=repo["path"], env=ENV, capture_output=True, text=True, check=False)
    actions = subprocess.run(
        args, cwd=repo["path"], env=ENV | {"GITHUB_ACTIONS": "true"}, capture_output=True, text=True, check=False
    )
    assert plain.returncode == actions.returncode == 0, actions.stdout + actions.stderr
    assert "::warning" not in plain.stdout
    annotations = [line for line in actions.stdout.splitlines() if line.startswith("::warning title=Owed upstream port::")]
    assert len(annotations) == 3
    assert any(repo["shas"]["u5"][:12] in line and "until 2026-10-13" in line for line in annotations)


def standalone_repo(tmp_path, name):
    path = tmp_path / name
    path.mkdir()
    git(path, "init", "-q", "-b", "up")
    for key, value in (("user.name", "Test"), ("user.email", "test@example.com"), ("commit.gpgsign", "false")):
        git(path, "config", key, value)
    commit(path, "root", {"r.py": lines("r"), "s.py": lines("s")})
    git(path, "tag", "v1.0")
    return path


def standalone_gate(path, ledger_text, *extra):
    (path / "UPSTREAM_SYNC.md").write_text(ledger_text)
    return run(
        path,
        "--base",
        "v1.0",
        "--head",
        "main",
        "--track",
        "refs/remotes/upstream/*",
        "--fail-on-unported",
        "UPSTREAM_SYNC.md",
        "--today",
        "2026-10-06",
        *extra,
    )


def test_a_merge_commit_with_its_own_changes_needs_a_decision(tmp_path):
    path = standalone_repo(tmp_path, "merges")
    side = commit(path, "side change", {"s.py": lines("s", overrides={2: "S2"})})
    git(path, "checkout", "-q", "-b", "dev", "v1.0")
    main_change = commit(path, "dev change", {"r.py": lines("r", overrides={2: "R2"})})
    commit(path, "clean tree", {"s.py": lines("s", overrides={2: "S2"})})
    clean = git(path, "commit-tree", git(path, "rev-parse", "HEAD^{tree}"), "-p", main_change, "-p", side, "-m", "clean")
    commit(path, "evil tree", {"s.py": lines("s", overrides={2: "S2", 9: "EVIL"})})
    evil = git(path, "commit-tree", git(path, "rev-parse", "HEAD^{tree}"), "-p", main_change, "-p", side, "-m", "evil")
    git(path, "update-ref", "refs/remotes/upstream/dev", evil)
    git(path, "update-ref", "refs/remotes/upstream/clean", clean)
    git(path, "checkout", "-q", "--orphan", "main", "v1.0")
    git(path, "commit", "-q", "-m", "fork root")
    decided = f"## Waived\n\n- `{side}` reason\n- `{main_change}` reason\n"
    red = standalone_gate(path, decided)
    assert red.returncode == 1, red.stdout + red.stderr
    assert "of them merge commits with their own changes: 1" in red.stdout
    assert evil[:12] in unported_block(red.stdout)
    assert clean[:12] not in red.stdout
    green = standalone_gate(path, decided + f"- `{evil}` the fork resolves this merge its own way\n")
    assert green.returncode == 0, green.stdout + green.stderr


def test_a_pick_that_keeps_the_lines_upstream_removed_is_not_a_port(tmp_path):
    path = standalone_repo(tmp_path, "removal")
    removal = commit(path, "drop the legacy path", {"r.py": lines("r", 6)})
    git(path, "update-ref", "refs/remotes/upstream/dev", removal)
    git(path, "checkout", "-q", "--orphan", "main", "v1.0")
    git(path, "commit", "-q", "-m", "fork root")
    git(path, "commit", "-q", "--allow-empty", "-m", f"port\n\n(cherry picked from commit {removal})")
    kept = standalone_gate(path, "## Waived\n")
    assert kept.returncode == 1, kept.stdout + kept.stderr
    assert f"{removal[:12]}: 4 of 4 upstream line(s) it adds or removes are not reflected in main" in kept.stdout
    commit(path, f"really port\n\n(cherry picked from commit {removal})", {"r.py": lines("r", 6)})
    ported = standalone_gate(path, "## Waived\n")
    assert ported.returncode == 0, ported.stdout + ported.stderr


def test_an_orphan_upstream_branch_is_tracked_when_the_base_is_upstream(tmp_path):
    path = standalone_repo(tmp_path, "orphan")
    fix = commit(path, "upstream fix", {"r.py": lines("r", overrides={1: "R1"})})
    git(path, "update-ref", "refs/remotes/upstream/dev", fix)
    orphan = git(path, "commit-tree", git(path, "rev-parse", f"{fix}^{{tree}}"), "-m", "v6 rewrite")
    git(path, "update-ref", "refs/remotes/upstream/v6", orphan)
    git(path, "checkout", "-q", "--orphan", "main", "v1.0")
    git(path, "commit", "-q", "-m", "fork root")
    result = standalone_gate(path, f"## Waived\n\n- `{fix}` reason\n")
    assert result.returncode == 1, result.stdout + result.stderr
    assert orphan[:12] in unported_block(result.stdout)
    assert "warning: skipped upstream/v6" not in result.stdout


def test_whitespace_changes_are_not_patch_identical_copies(tmp_path):
    path = standalone_repo(tmp_path, "whitespace")
    guarded = commit(path, "guarded call", {"g.py": "if allowed:\n    run()\n"})
    git(path, "update-ref", "refs/remotes/upstream/guarded", guarded)
    git(path, "checkout", "-q", "-b", "dev", "v1.0")
    unguarded = commit(path, "unguarded call", {"g.py": "if allowed:\nrun()\n"})
    git(path, "update-ref", "refs/remotes/upstream/dev", unguarded)
    git(path, "checkout", "-q", "--orphan", "main", "v1.0")
    git(path, "commit", "-q", "-m", "fork root")
    result = standalone_gate(path, f"## Waived\n\n- `{guarded}` the fork never calls run\n")
    assert result.returncode == 1, result.stdout + result.stderr
    assert unguarded[:12] in unported_block(result.stdout)


@pytest.mark.parametrize("reason", ["UNMERGED WIP: only on feat/x", "**UNMERGED-WIP**: only on feat/x", "Pending: feat/x"])
def test_a_misspelled_conditional_tag_still_ends_when_upstream_merges(repo, tmp_path, reason):
    result = parity(
        repo,
        covering_ledger(tmp_path, repo, reason),
        "--track",
        "refs/upstream-release/merged",
        "--release-ref",
        "refs/upstream-release/merged",
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "is waived only while unmerged" in result.stdout


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("##Port queue\n\n- `{u3}` port it security: no\n", "is not a Markdown heading; put a space after the hashes"),
        (
            "## Port queue\n\n- `{u3}` until 2026-10-08 or so; security: no until: 2026-10-10\n",
            "names 2 until dates; keep exactly one",
        ),
        ("## Waived\n\n<!--\n- `{u3}` hidden decision\n-->\n", ""),
        (
            "## Waived\n\n- `{u3}` reason\n\n## Port queue\n\n- `{u3}` port it security: no until: 2026-10-10\n",
            "is also decided differently",
        ),
        ("## Port queue\n\n- `{u3}` port it until: 2026-10-10\n", "needs exactly one security: yes|no classification"),
        ("## Port queue\n\n- `{u3}` security: no until: 2026-10-10\n", "`{u3}` has no reason"),
        ("## Port queue\n\n- `{u3}` — security: yes. until: 2026-10-10\n", "`{u3}` has no reason"),
        ("## Waived\n\n- `{u3}` until: 2026-10-10\n", "`{u3}` has no reason"),
        ("## Port queue\n\n- `{u3}` (security: no) until: 2026-10-10\n", "`{u3}` has no reason"),
        ("## Port queue\n\n- `{u3}` security: no (until: 2026-10-10)\n", "`{u3}` has no reason"),
        ("## Port queue\n\n- `{u3}` **security: no** until: 2026-10-10\n", "`{u3}` has no reason"),
        ("## Port queue\n\n- `{u3}` security: no! until: 2026-10-10\n", "`{u3}` has no reason"),
        ("## Port queue\n\n- `{u3}` security: no until: 2026-10-10 ?\n", "`{u3}` has no reason"),
        ("## Port queue\n\n- `{u3}` security: no / until: 2026-10-10\n", "`{u3}` has no reason"),
        ("## Port queue\n\n- `{u3}` security: no\u00a0until: 2026-10-10\n", "`{u3}` has no reason"),
        ("## Waived\n\n- `{u3}` (until: 2026-10-10)\n", "`{u3}` has no reason"),
        ("## Port queue\n\n- `{u3}` _security: no_ until: 2026-10-10\n", "needs exactly one security: yes|no classification"),
        (
            "## Port queue\n\n- `{u3}` port it security: yes security: no until: 2026-10-10\n",
            "needs exactly one security: yes|no classification",
        ),
    ],
)
def test_ledger_forms_that_must_not_count_as_a_decision(repo, tmp_path, text, message):
    shas = repo["shas"]
    rest = "## Waived\n\n" + "".join(f"- `{shas[k]}` reason\n" for k in ("u5", "f1", "f2")) + "\n"
    result = parity(repo, ledger(tmp_path, rest + text.format(u3=shas["u3"])), "--today", "2026-10-06")
    assert result.returncode == 1, result.stdout + result.stderr
    assert message.format(u3=shas["u3"]) in result.stdout


def security_repo(tmp_path):
    path = standalone_repo(tmp_path, "security")
    shas = {
        "auth": commit(path, "fix(auth): invalidate tokens for replaced admin accounts (#938)", {"r.py": lines("r", overrides={1: "R1"})}),
        "plain": commit(path, "chore(deps): bump a library", {"s.py": lines("s", overrides={1: "S1"})}),
        "feature": commit(path, "feat(subscription): add a header option", {"r.py": lines("r", overrides={1: "R1", 2: "R2"})}),
    }
    git(path, "update-ref", "refs/remotes/upstream/dev", shas["feature"])
    git(path, "checkout", "-q", "-b", "copy", "v1.0")
    git(path, "cherry-pick", shas["auth"])
    git(path, "commit", "-q", "--amend", "-m", "fix(auth): the same token fix on a release branch")
    shas["copy"] = git(path, "rev-parse", "HEAD")
    git(path, "update-ref", "refs/remotes/upstream/release", shas["copy"])
    git(path, "checkout", "-q", "--orphan", "main", "v1.0")
    git(path, "commit", "-q", "-m", "fork root")
    return path, shas


def test_an_unclassified_queue_entry_fails_the_gate(tmp_path):
    path, shas = security_repo(tmp_path)
    queued = (
        "## Port queue\n\n"
        f"- `{shas['auth']}` MUST-PORT: token invalidation security: yes until: 2026-10-13\n"
        f"- `{shas['plain']}` port the bump until: 2026-10-13\n"
        f"- `{shas['feature']}` port the option security: no until: 2026-10-13\n"
    )
    red = standalone_gate(path, queued)
    assert red.returncode == 1, red.stdout + red.stderr
    assert f"`{shas['plain']}` in the port queue needs exactly one security: yes|no classification" in red.stdout
    assert shas["plain"][:12] in unported_block(red.stdout)


def test_a_security_yes_entry_gets_no_grace_even_with_an_innocuous_subject(tmp_path):
    path, shas = security_repo(tmp_path)
    git(path, "update-ref", "refs/remotes/upstream/dev", shas["plain"])
    queued = (
        "## Port queue\n\n"
        f"- `{shas['plain']}` port the bump; it ships an OpenSSL security release security: yes until: 2026-10-13\n"
    )
    red = standalone_gate(path, queued)
    assert red.returncode == 1, red.stdout + red.stderr
    assert f"`{shas['plain'][:12]}` is classified security: yes; a security port gets no grace" in red.stdout
    assert shas["plain"][:12] in unported_block(red.stdout)


def test_a_keyword_only_non_security_commit_keeps_grace_but_is_flagged(tmp_path):
    path = standalone_repo(tmp_path, "session-tab")
    ui = commit(path, "feat(ui): remember the last open session tab", {"r.py": lines("r", overrides={1: "R1"})})
    git(path, "update-ref", "refs/remotes/upstream/dev", ui)
    git(path, "checkout", "-q", "--orphan", "main", "v1.0")
    git(path, "commit", "-q", "-m", "fork root")
    green = standalone_gate(path, f"## Port queue\n\n- `{ui}` ships a UI tab, not security security: no until: 2026-10-13\n")
    assert green.returncode == 0, green.stdout + green.stderr
    assert tally(green.stdout, OWED_LABEL) == 1
    assert f"`{ui[:12]}` is classified security: no but it names 'session'; re-review the classification" in green.stdout
    assert "is classified security: yes" not in green.stdout


def test_a_security_commit_is_green_once_ported_or_waived(tmp_path):
    path, shas = security_repo(tmp_path)
    queue = (
        "## Port queue\n\n"
        f"- `{shas['plain']}` port the bump security: no until: 2026-10-13\n"
        f"- `{shas['feature']}` port it security: no until: 2026-10-13\n"
    )
    waived = standalone_gate(path, f"## Waived\n\n- `{shas['auth']}` the fork rejects tokens of replaced admins in its own code\n\n" + queue)
    assert waived.returncode == 0, waived.stdout + waived.stderr
    assert tally(waived.stdout, OWED_LABEL) == 2
    git(path, "cherry-pick", "-x", shas["auth"])
    ported = standalone_gate(path, queue + f"- `{shas['auth']}` MUST-PORT: token invalidation security: yes until: 2026-10-13\n")
    assert ported.returncode == 0, ported.stdout + ported.stderr
    assert f"`{shas['auth'][:12]}` is ported now; delete it from the port queue" in ported.stdout


@pytest.mark.parametrize("queue_first", [True, False])
def test_a_port_queue_line_next_to_a_ported_by_design_line_is_stale(repo, tmp_path, queue_first):
    shas = repo["shas"]
    queue = f"## Port queue\n\n- `{shas['u3']}` port it security: no until: 2026-10-13\n\n"
    design = f"## Ported by design\n\n- `{shas['u3']}` adapted by hand in the fork\n\n"
    rest = "## Waived\n\n" + "".join(f"- `{shas[k]}` reason\n" for k in ("u5", "f1", "f2")) + "\n"
    text = rest + (queue + design if queue_first else design + queue)
    result = parity(repo, ledger(tmp_path, text), "--today", "2026-10-06")
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"`{shas['u3'][:12]}` is ported by design now; delete it from the port queue" in result.stdout
    assert tally(result.stdout, OWED_LABEL) == 0


def test_a_queued_port_that_landed_is_flagged_for_deletion(repo, tmp_path):
    queue = [(key, "port until: 2026-10-13") for key in ("u1", "dup", "u3", "u5")]
    result = parity(repo, queue_ledger(tmp_path, repo, queue), "--today", "2026-10-06")
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"`{repo['shas']['u1'][:12]}` is ported now; delete it from the port queue" in result.stdout
    assert f"`{repo['shas']['dup'][:12]}` is ported now as a patch-identical copy; delete it" in result.stdout
    assert tally(result.stdout, "patch-id equivalent of a covered commit") == 1
    assert tally(result.stdout, OWED_LABEL) == 3
    assert tally(result.stdout, "ported via cherry-pick -x trailer") == 1


def test_removing_one_decision_turns_the_gate_red(repo, tmp_path):
    shas = repo["shas"]
    queue = [("u3", "port until: 2026-10-13"), ("u5", "port until: 2026-10-13")]
    green = parity(repo, queue_ledger(tmp_path, repo, queue), "--today", "2026-10-06")
    assert green.returncode == 0, green.stdout + green.stderr
    for dropped_queue, dropped_waiver in ((queue[:1], ("f1", "f2")), (queue, ("f1",))):
        red = parity(repo, queue_ledger(tmp_path, repo, dropped_queue, dropped_waiver), "--today", "2026-10-06")
        assert red.returncode == 1, red.stdout + red.stderr
        block = unported_block(red.stdout)
        missing = {shas[key] for key in ("u5", "f2") if shas[key][:12] in block}
        assert len(missing) == 1
        assert "Upstream parity gate FAILED: 1 upstream commit(s) neither ported nor listed" in red.stdout


def _snapshot_repo(tmp_path):
    path = tmp_path / "snapshot-repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "up")
    for key, value in (("user.name", "Test"), ("user.email", "test@example.com"), ("commit.gpgsign", "false")):
        git(path, "config", key, value)
    commit(path, "root", {"r.py": lines("r")})
    git(path, "tag", "v1.0")
    git(path, "checkout", "-q", "-b", "feature")
    feature = commit(path, "feature work", {"f.py": lines("f", 2)})
    git(path, "update-ref", "refs/remotes/upstream/feature", feature)
    git(path, "update-ref", "refs/remotes/upstream/main", git(path, "rev-parse", "v1.0"))
    git(path, "checkout", "-q", "--orphan", "main", "v1.0")
    git(path, "commit", "-q", "-m", "fork root")
    (path / "UPSTREAM_SYNC.md").write_text("## Ported by design\n\n## Waived\n")
    return path, feature


def _gate(path, *extra):
    return run(
        path,
        "--base",
        "v1.0",
        "--head",
        "main",
        "--track",
        "refs/remotes/upstream/*",
        "--fail-on-unported",
        "UPSTREAM_SYNC.md",
        "--refs-snapshot",
        ".upstream-refs",
        *extra,
    )


def test_an_upstream_head_missing_from_the_snapshot_fails_the_gate(tmp_path):
    path, feature = _snapshot_repo(tmp_path)
    (path / "UPSTREAM_SYNC.md").write_text(f"## Waived\n\n- `{feature}` feature branch work, not merged\n")
    result = _gate(path)
    assert result.returncode == 1
    assert f"upstream head upstream/feature at {feature[:12]} is not recorded" in result.stdout
    assert "record the reviewed heads by re-running with --write-refs-snapshot, which writes .upstream-refs" in result.stdout
    recorded = _gate(path, "--write-refs-snapshot")
    assert recorded.returncode == 0, recorded.stdout + recorded.stderr
    assert f"{feature} upstream/feature" in (path / ".upstream-refs").read_text()


def test_a_deleted_upstream_branch_stays_tracked_through_the_snapshot(tmp_path):
    path, feature = _snapshot_repo(tmp_path)
    (path / ".upstream-refs").write_text(f"{feature} upstream/feature\n")
    git(path, "update-ref", "-d", "refs/remotes/upstream/feature")
    recorded_main = git(path, "rev-parse", "v1.0")
    (path / ".upstream-refs").write_text(f"{feature} upstream/feature\n{recorded_main} upstream/main\n")

    result = _gate(path)

    assert result.returncode == 1
    assert f"upstream/feature@{feature[:12]} (snapshot)" in result.stdout
    assert feature[:12] in unported_block(result.stdout)
    (path / "UPSTREAM_SYNC.md").write_text(f"## Waived\n\n- `{feature}` deleted upstream without merge\n")
    assert _gate(path).returncode == 0


def test_a_snapshot_head_that_is_not_in_the_clone_is_an_error(tmp_path):
    path, _ = _snapshot_repo(tmp_path)
    ghost = "0123456789abcdef0123456789abcdef01234567"
    live = "".join(
        f"{git(path, 'rev-parse', ref)} upstream/{ref.rsplit('/', 1)[-1]}\n"
        for ref in ("refs/remotes/upstream/feature", "refs/remotes/upstream/main")
    )
    (path / ".upstream-refs").write_text(live + f"{ghost} upstream/gone\n")
    result = _gate(path)
    assert result.returncode == 1
    assert f"snapshot head {ghost[:12]} of upstream/gone is not in this clone" in result.stdout


def covering_ledger(tmp_path, repo, f1_reason="rewritten as fork code"):
    shas = repo["shas"]
    return ledger(
        tmp_path,
        "## Ported by design\n\n"
        f"- `{shas['f2']}` folded into the fork feature\n\n"
        "## Waived\n\n"
        f"- `{shas['f1']}` {f1_reason}\n"
        f"- `{shas['u3']}` the fork keeps c as it is\n"
        f"- `{shas['u5']}` the fork bridge has its own floor\n",
    )


CONDITIONAL_REASONS = [
    "UNMERGED-WIP: only on upstream feat/x",
    "pending: tracked feature branch upstream/feat/x, port it when it merges. until: 2026-10-25",
]


@pytest.mark.parametrize("reason", CONDITIONAL_REASONS)
def test_a_conditional_waiver_holds_while_the_commit_stays_off_the_release_lines(repo, tmp_path, reason):
    result = parity(repo, covering_ledger(tmp_path, repo, reason))
    assert result.returncode == 0, result.stdout + result.stderr
    assert tally(result.stdout, "waived (ledger)") == 3


@pytest.mark.parametrize("reason", CONDITIONAL_REASONS)
def test_a_conditional_waiver_expires_when_upstream_merges_the_commit(repo, tmp_path, reason):
    result = parity(
        repo,
        covering_ledger(tmp_path, repo, reason),
        "--track",
        "refs/upstream-release/merged",
        "--release-ref",
        "refs/upstream-release/merged",
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "is waived only while unmerged, but upstream now carries it on upstream-release/merged" in result.stdout
    assert repo["shas"]["f1"][:12] in unported_block(result.stdout)


@pytest.mark.parametrize("reason", CONDITIONAL_REASONS)
def test_a_conditional_waiver_does_not_cover_a_rebased_copy_on_a_release_line(repo, tmp_path, reason):
    result = parity(
        repo,
        covering_ledger(tmp_path, repo, reason),
        "--track",
        "refs/upstream-release/rebased",
        "--release-ref",
        "refs/upstream-release/rebased",
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert repo["shas"]["f1_copy"][:12] in unported_block(result.stdout)
    assert "which is waived only while unmerged, so the copy needs its own decision" in result.stdout


def test_a_plain_waiver_still_covers_a_rebased_copy(repo, tmp_path):
    result = parity(
        repo,
        covering_ledger(tmp_path, repo),
        "--track",
        "refs/upstream-release/rebased",
        "--release-ref",
        "refs/upstream-release/rebased",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert tally(result.stdout, "patch-id equivalent of a covered commit") == 3


def test_a_conditional_waiver_without_any_release_ref_is_an_error(repo, tmp_path):
    result = parity(
        repo,
        covering_ledger(tmp_path, repo, CONDITIONAL_REASONS[0]),
        "--release-ref",
        "refs/nowhere/*",
    )
    assert result.returncode == 1
    assert "UNMERGED-WIP and pending waivers need at least one existing --release-ref" in result.stdout


def test_an_empty_upstream_commit_needs_no_decision(repo, tmp_path):
    result = parity(repo, covering_ledger(tmp_path, repo), "--track", "refs/upstream-release/empty")
    assert result.returncode == 0, result.stdout + result.stderr
    assert tally(result.stdout, "changes no file") == 1
    assert repo["shas"]["empty"][:12] not in result.stdout


def test_a_fork_based_gate_without_shared_history_never_passes_vacuously(repo, tmp_path):
    empty = ledger(tmp_path, "## Waived\n")
    common = ["--base", "main", "--head", "main", "--track", "refs/remotes/upstream/*", "--fail-on-unported", empty]
    vacuous = run(repo["path"], *common, "--upstream-head", "none")
    assert vacuous.returncode == 2
    assert "pass --unrelated-history" in vacuous.stderr
    related = run(repo["path"], *common, "--upstream-head", "none", "--unrelated-history")
    assert related.returncode == 1, related.stdout + related.stderr
    shas = repo["shas"]
    block = unported_block(related.stdout)
    assert {sha for sha in shas.values() if sha[:12] in block} == {
        shas["u3"],
        shas["u5"],
        shas["f1"],
        shas["f2"],
        shas["g"],
    }
    assert tally(related.stdout, "patch-id equivalent already in the base") == 1
    fork_sync_point = run(repo["path"], *common, "--unrelated-history", "--upstream-head", "main")
    assert fork_sync_point.returncode == 2
    assert "it must be an upstream commit, not a fork commit" in fork_sync_point.stderr


def test_the_sync_point_must_be_upstream_history_on_a_tracked_ref(repo, tmp_path):
    fork_point = run(repo["path"], "--base", "v1.0", "--head", "main", "--upstream-head", "main")
    assert fork_point.returncode == 2
    assert "it must be an upstream commit, not a fork commit" in fork_point.stderr
    untracked = parity(repo, covering_ledger(tmp_path, repo), "--upstream-head", "refs/upstream-release/empty")
    assert untracked.returncode == 1
    assert "is not on any tracked upstream ref" in untracked.stdout
    behind = parity(repo, covering_ledger(tmp_path, repo), "--upstream-head", repo["shas"]["u4"])
    assert behind.returncode == 0, behind.stdout + behind.stderr
    assert "is 1 commit(s) behind upstream/dev" in behind.stdout


@pytest.mark.parametrize(
    ("head", "args", "code", "message"),
    [
        ("main", ["--npm-latest", "@pasarguard/xray-config-kit=0.3.8"], 0, "Npm package gate OK"),
        ("main", ["--npm-latest", "@pasarguard/xray-config-kit=0.3.9"], 1, "0.3.8 is behind upstream 0.3.9"),
        (
            "main",
            ["--npm-latest", "@pasarguard/xray-config-kit=0.3.8", "--upstream-head", "refs/upstream-release/npm-floor"],
            1,
            "(from '^0.3.9' in dashboard/package.json on refs/upstream-release/npm-floor)",
        ),
        ("npm-waiver-ok", ["--npm-latest", "@pasarguard/xray-config-kit=0.3.9"], 0, "waived by upstream-npm-waivers"),
        ("npm-waiver-ok", ["--npm-latest", "@pasarguard/xray-config-kit=0.3.10"], 1, "is stale: it waives 0.3.9"),
        (
            "main",
            ["--npm-latest", "@pasarguard/xray-config-kit=0.3.8", "--upstream-head", "refs/upstream-release/npm-lock"],
            1,
            "0.3.8 is behind upstream 0.3.9 (from dashboard/bun.lock on refs/upstream-release/npm-lock)",
        ),
        (
            "npm-waiver-dated",
            ["--npm-latest", "@pasarguard/xray-config-kit=0.3.9", "--today", "2026-10-25"],
            0,
            "waived by upstream-npm-waivers.toml for 0.3.9",
        ),
        (
            "npm-waiver-dated",
            ["--npm-latest", "@pasarguard/xray-config-kit=0.3.9", "--today", "2026-10-26"],
            1,
            "expired on 2026-10-25; re-check it",
        ),
        (
            "npm-nested-behind",
            ["--npm-latest", "@pasarguard/xray-config-kit=0.3.8"],
            1,
            "@pasarguard/core-kit/@pasarguard/xray-config-kit: 0.3.7 is behind upstream 0.3.8",
        ),
    ],
)
def test_npm_gate(repo, head, args, code, message):
    result = run(repo["path"], "--head", head, "--check-npm", *args)
    assert result.returncode == code, result.stdout + result.stderr
    assert message in result.stdout
    assert "@pasarguard/core-kit: built from file:../../core-kit, not version-gated here" in result.stdout
    assert "zod" not in result.stdout


def test_npm_gate_usage_errors(repo):
    bad_override = run(repo["path"], "--check-npm", "--npm-latest", "nonsense")
    assert bad_override.returncode == 2
    missing = run(repo["path"], "--check-npm", "--npm-latest", "x=1", "--npm-lock", "nowhere/bun.lock")
    assert missing.returncode == 2
    assert "nowhere/bun.lock not found" in missing.stdout


def test_a_reverted_cherry_pick_no_longer_counts_as_ported(repo, tmp_path):
    shas = repo["shas"]
    result = run(
        repo["path"],
        "--base",
        "v1.0",
        "--head",
        "reverted-pick",
        "--track",
        "refs/remotes/upstream/*",
        "--track",
        "v1.*",
        "--fail-on-unported",
        covering_ledger(tmp_path, repo),
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"{shas['u1'][:12]}: every fork commit carrying its trailer is reverted" in result.stdout
    assert shas["u1"][:12] in unported_block(result.stdout)


def test_a_cherry_pick_the_fork_changed_needs_a_decision(repo, tmp_path):
    shas = repo["shas"]
    text = "## Waived\n\n" + "".join(f"- `{shas[k]}` reason\n" for k in ("u3", "u5", "f1", "f2"))
    unlisted = parity(repo, ledger(tmp_path, text, adapted=False))
    assert unlisted.returncode == 1
    block = unported_block(unlisted.stdout)
    assert {sha for sha in shas.values() if sha[:12] in block} == {shas["u2"], shas["u4"]}
    assert f"{shas['u2'][:12]}: 1 of 4 upstream line(s) it adds or removes are not reflected in main" in unlisted.stdout
    assert "needs a Ported by design or Waived entry" in unlisted.stdout
    listed = parity(repo, ledger(tmp_path, text))
    assert listed.returncode == 0, listed.stdout + listed.stderr
    assert "covered by its ledger entry" in listed.stdout


@pytest.mark.parametrize("sync_point", ["first", "second"])
def test_a_pick_whose_lines_upstream_changed_later_still_counts(tmp_path, sync_point):
    path = tmp_path / "later"
    path.mkdir()
    git(path, "init", "-q", "-b", "up")
    for key, value in (("user.name", "Test"), ("user.email", "test@example.com"), ("commit.gpgsign", "false")):
        git(path, "config", key, value)
    commit(path, "root", {"r.py": lines("r")})
    git(path, "tag", "v1.0")
    first = commit(path, "upstream sets r2", {"r.py": lines("r", overrides={2: "R2-first"})})
    second = commit(path, "upstream changes r2 again", {"r.py": lines("r", overrides={2: "R2-second"})})
    git(path, "update-ref", "refs/remotes/upstream/dev", second)
    git(path, "checkout", "-q", "--orphan", "main", "v1.0")
    git(path, "commit", "-q", "-m", "fork root")
    git(path, "cherry-pick", "-x", first)
    git(path, "cherry-pick", "-x", second)
    (path / "UPSTREAM_SYNC.md").write_text("## Ported by design\n\n## Waived\n")
    result = run(
        path,
        "--base",
        "v1.0",
        "--head",
        "main",
        "--upstream-head",
        first if sync_point == "first" else second,
        "--track",
        "refs/remotes/upstream/*",
        "--fail-on-unported",
        "UPSTREAM_SYNC.md",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert tally(result.stdout, "ported via cherry-pick -x trailer") == 2


@pytest.mark.parametrize("line", ["#waived", "##ported by design", "  #Waived  #"])
def test_a_section_name_without_a_space_after_its_hashes_fails_the_gate(tmp_path, line):
    path, shas = security_repo(tmp_path)
    git(path, "update-ref", "refs/remotes/upstream/dev", shas["plain"])
    ledger_text = (
        "## Port queue\n\n"
        f"- `{shas['auth']}` token invalidation security: no until: 2026-10-13\n"
        f"{line}\n"
        f"- `{shas['plain']}` bump the library security: yes until: 2026-10-13\n"
    )
    result = standalone_gate(path, ledger_text)
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"`{line.strip()}` is not a Markdown heading; put a space after the hashes" in result.stdout


def test_a_hash_line_that_names_no_section_is_text_inside_the_port_queue(tmp_path):
    path, shas = security_repo(tmp_path)
    git(path, "update-ref", "refs/remotes/upstream/dev", shas["plain"])
    ledger_text = (
        "## Port queue\n\n"
        f"- `{shas['auth']}` token invalidation security: no until: 2026-10-13\n"
        "#Waived: moved from the queue\n"
        f"- `{shas['plain']}` bump the library security: yes until: 2026-10-13\n"
    )
    result = standalone_gate(path, ledger_text)
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"`{shas['plain'][:12]}` is classified security: yes; a security port gets no grace" in result.stdout
    assert shas["plain"][:12] in unported_block(result.stdout)


def test_a_wrapped_line_that_starts_with_an_issue_number_keeps_the_section_open(tmp_path):
    path, shas = security_repo(tmp_path)
    git(path, "update-ref", "refs/remotes/upstream/dev", shas["feature"])
    queue = (
        f"- `{shas['auth']}` token invalidation security: no until: 2026-10-13\n"
        f"- `{shas['plain']}` bump the library security: no until: 2026-10-13\n"
        f"- `{shas['feature']}` add a header option security: no until: 2026-10-13\n"
    )
    control = standalone_gate(path, "## Port queue\n\n" + queue)
    assert control.returncode == 0, control.stdout + control.stderr
    wrapped = standalone_gate(
        path,
        "## Port queue\n\nIntro text that wraps onto a line beginning with an issue number, like\n"
        "#879 and #617, which is prose and not a heading.\n\n" + queue,
    )
    assert wrapped.returncode == 0, wrapped.stdout + wrapped.stderr
    assert tally(wrapped.stdout, OWED_LABEL) == tally(control.stdout, OWED_LABEL) >= 3



CRAFTED_WAIVED = "## Waived\n\n- `{auth}` the fork rejects tokens of replaced admins in its own code\n"
CRAFTED_QUEUE = "\n## Port queue\n\n- `{feature}` add the header option security: no until: 2026-10-13\n"
NOT_A_HEADING = "is not a Markdown heading; put a space after the hashes"
NESTED_ENTRY = "an entry holds one paragraph only, with no nested block"
REPEATED_SECTION = "section already starts at line"
HTML_IN_LEDGER = "HTML is not allowed in a ledger"
UNSUPPORTED_BLOCK = "block is not allowed in a ledger section"
CRAFTED_LEDGER_LINES = {
    "hash-waived": ("", "#waived\n- `{plain}` crafted waiver\n", NOT_A_HEADING),
    "hash-hash-waived": ("", "##Waived\n- `{plain}` crafted waiver\n", NOT_A_HEADING),
    "hash-hash-ported": ("", "##Ported by design\n- `{plain}` crafted port\n", NOT_A_HEADING),
    "indented-heading-in-entry": ("", "  ## Waived\n- `{plain}` crafted waiver\n", NESTED_ENTRY),
    "indented-ported-heading": ("", "   ### Ported by design\n- `{plain}` crafted port\n", NESTED_ENTRY),
    "waived-again-after-queue": ("", "\n## Waived\n\n- `{plain}` crafted waiver\n", REPEATED_SECTION),
    "waived-in-continuation": ("", "  wrapped reason of the entry above\n## Waived\n- `{plain}` crafted\n", REPEATED_SECTION),
    "ported-phrase-prose": ("", "\nPorted by design: `{plain}` is carried by the fork\n", None),
    "ported-phrase-ordered-list": ("", "\n1. `{plain}` ported by design\n", UNSUPPORTED_BLOCK),
    "ported-phrase-nested-bullet": ("", "  - `{plain}` ported by design\n", NESTED_ENTRY),
    "ported-phrase-before-sha": ("", "- Ported by design `{plain}`\n", "entry is not in the form"),
    "backtick-fence": ("```\n- `{plain}` waived inside a fence\n```\n", "", UNSUPPORTED_BLOCK),
    "tilde-fence": ("~~~text\n- `{plain}` waived inside a fence\n~~~\n", "", UNSUPPORTED_BLOCK),
    "fence-hides-a-heading": ("", "```\n## Waived\n```\n- `{plain}` crafted waiver\n", UNSUPPORTED_BLOCK),
    "html-comment": ("<!--\n- `{plain}` hidden waiver\n-->\n", "", HTML_IN_LEDGER),
    "unclosed-html-comment": ("<!--\n- `{plain}` hidden waiver\n", "", HTML_IN_LEDGER),
    "queue-and-waived": (
        "- `{plain}` crafted waiver\n",
        "- `{plain}` bump the library security: yes until: 2026-10-13\n",
        "is also decided differently",
    ),
}


def crafted_ledger(shas, in_waived, in_queue):
    return (CRAFTED_WAIVED + in_waived + CRAFTED_QUEUE + in_queue).format(**shas)


def test_the_crafted_ledger_baseline_is_green_once_the_commit_is_really_decided(tmp_path):
    path, shas = security_repo(tmp_path)
    decided = crafted_ledger(shas, "", "\n## Ported by design\n\n- `{plain}` the fork carries the bump in its own lock\n")
    result = standalone_gate(path, decided)
    assert result.returncode == 0, result.stdout + result.stderr
    undecided = standalone_gate(path, crafted_ledger(shas, "", ""))
    assert undecided.returncode == 1
    assert shas["plain"][:12] in unported_block(undecided.stdout)


@pytest.mark.parametrize("case", sorted(CRAFTED_LEDGER_LINES))
def test_a_crafted_ledger_line_cannot_decide_an_unported_commit(tmp_path, case):
    path, shas = security_repo(tmp_path)
    in_waived, in_queue, error = CRAFTED_LEDGER_LINES[case]
    result = standalone_gate(path, crafted_ledger(shas, in_waived, in_queue))
    assert result.returncode == 1, result.stdout + result.stderr
    if error:
        assert error in result.stdout, result.stdout
    else:
        assert shas["plain"][:12] in unported_block(result.stdout), result.stdout


def b7_adv_battery(shas):
    a, b = shas["feature"], shas["plain"]
    head = f"## Ported by design\n\n- `{shas['auth']}` the fork rejects tokens of replaced admins in its own code\n\n"
    qa = f"- `{a}` until: 2026-10-10 security: no: small tweak\n"
    qb = f"- `{b}` until: 2026-10-10 security: yes: bump fix, port now\n"
    wa = f"- `{a}` waived: fork does not use it\n"
    return {
        "control_queue": (head + "## Port queue\n\n" + qa + qb, "red"),
        "hash_no_space": (head + "## Port queue\n\n" + qa + "#waived\n" + qb, NOT_A_HEADING),
        "setext_port_queue": (head + "## Waived\n\n" + wa + "\nPort queue\n----------\n\n" + qb, "setext heading"),
        "indented_1sp_atx": (head + "## Waived\n\n" + wa + "\n ## Port queue\n\n" + qb, "heading is indented"),
        "blockquote_heading": (head + "## Waived\n\n" + wa + "\n> ## Port queue\n\n" + qb, UNSUPPORTED_BLOCK),
        "fenced_heading": (head + "## Port queue\n\n" + qa + "\n~~~\n## Waived\n~~~\n\n" + qb, UNSUPPORTED_BLOCK),
        "backtick_fence_heading": (head + "## Port queue\n\n" + qa + "\n```\n## Waived\n```\n\n" + qb, UNSUPPORTED_BLOCK),
        "html_comment_in_code_span": (
            head
            + "## Waived\n\n"
            + f"- `{a}` waived: it adds `<!--` markers to templates\n"
            + "\n## Port queue\n\n`-->` closes such a marker\n\n"
            + qb,
            "red",
        ),
        "u2028_split": (head + "## Port queue\n\n" + qa + "\nSee the notes below.\u2028## Waived\n\n" + qb, "U+2028"),
        "u0085_split": (head + "## Port queue\n\n" + qa + "\nSee the notes below.\u0085## Waived\n\n" + qb, "U+0085"),
        "formfeed_split": (head + "## Port queue\n\n" + qa + "\nSee the notes below.\x0c## Waived\n\n" + qb, "U+000C"),
        "nbsp_after_hashes": (head + "## Port queue\n\n" + qa + "##\u00a0Waived\n" + qb, "red"),
        "zero_width_subheading": (head + "## Waived\n\n" + wa + "\n### Port\u200b queue\n\n" + qb, "U+200B"),
        "homoglyph_subheading": (head + "## Waived\n\n" + wa + "\n### P\u043ert queue\n\n" + qb, "non-ASCII"),
        "plain_subheading_named_like_queue": (
            head + "## Waived\n\n" + wa + "\n### Port queue (security)\n\n" + qb,
            "names a ledger section",
        ),
        "unterminated_html_comment": (head + "## Port queue\n\n" + qa + "\n<!--\n## Waived\n\n" + qb, HTML_IN_LEDGER),
        "unterminated_comment_at_line_start": (
            head + "## Port queue\n\n" + qa + "\n<!-- note\n## Waived\n" + qb,
            HTML_IN_LEDGER,
        ),
        "script_block_heading": (head + "## Port queue\n\n" + qa + "\n<script>\n## Waived\n</script>\n\n" + qb, HTML_IN_LEDGER),
        "style_block_heading": (head + "## Port queue\n\n" + qa + "\n<style>\n## Waived\n</style>\n\n" + qb, HTML_IN_LEDGER),
        "pre_block_heading": (head + "## Port queue\n\n" + qa + "\n<pre>\n## Waived\n</pre>\n\n" + qb, HTML_IN_LEDGER),
        "nbsp_list_marker": (head + "## Waived\n\n" + wa + "-\u00a0`" + b + "` waived: looks like prose\n", "unported"),
        "crlf_control": ((head + "## Port queue\n\n" + qa + qb).replace("\n", "\r\n"), "red"),
        "trailing_hash_close": (head + "## Waived\n\n" + wa + "\n## Port queue\n\n" + qb + "## Waived ##\n" + qb, REPEATED_SECTION),
        "tab_after_hashes": (head + "## Waived\n\n" + wa + "\n## Port queue\n\n" + qb + "##\tWaived\n" + qb, REPEATED_SECTION),
    }


@pytest.mark.parametrize(
    "case",
    sorted(b7_adv_battery({"auth": "0" * 40, "plain": "1" * 40, "feature": "2" * 40})),
)
def test_every_construct_of_the_parser_battery_fails_closed(tmp_path, case):
    path, shas = security_repo(tmp_path)
    text, expected = b7_adv_battery(shas)[case]
    result = standalone_gate(path, text)
    assert result.returncode == 1, result.stdout + result.stderr
    if expected == "red":
        assert f"`{shas['plain'][:12]}` is classified security: yes; a security port gets no grace" in result.stdout
        assert shas["plain"][:12] in unported_block(result.stdout)
    elif expected == "unported":
        assert shas["plain"][:12] in unported_block(result.stdout), result.stdout
    else:
        assert expected in result.stdout, result.stdout


@pytest.mark.parametrize(
    "ledger",
    ["UPSTREAM_SYNC.md", *sorted(str(p.relative_to(SCRIPT.parent.parent)) for p in (SCRIPT.parent.parent / "upstream-forks").glob("*.md"))],
)
def test_the_real_ledgers_have_no_structural_errors(ledger):
    sys.path.insert(0, str(SCRIPT.parent))
    import upstream_inventory

    root = SCRIPT.parent.parent
    items, errors = upstream_inventory.ledger_items(ledger, (root / ledger).read_text())
    assert errors == []
    if ledger == "UPSTREAM_SYNC.md":
        assert len(items) > 50



@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("security: no until: 2026-10-13", "security: yes until: 2026-10-13"),
        ("security: yes until: 2026-10-13", "security: no until: 2026-10-13"),
        ("security: no until: 2026-10-13", "security: no until: 2026-10-20"),
    ],
)
@pytest.mark.parametrize("short", [False, True])
def test_two_queue_lines_for_one_commit_that_disagree_fail_the_gate(tmp_path, first, second, short):
    path, shas = security_repo(tmp_path)
    plain = shas["plain"]
    ledger_text = crafted_ledger(
        shas,
        "",
        f"- `{plain[:12] if short else plain}` bump the library {first}\n- `{plain}` bump the library {second}\n",
    )
    result = standalone_gate(path, ledger_text)
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"{plain[:12]} is also decided differently" in result.stdout


def test_two_identical_queue_lines_for_one_commit_are_only_a_warning(tmp_path):
    path, shas = security_repo(tmp_path)
    line = f"- `{shas['plain']}` bump the library security: no until: 2026-10-13\n"
    result = standalone_gate(path, crafted_ledger(shas, "", line + line))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "is already listed at" in result.stdout



@pytest.mark.parametrize("trailer", ["empty", "reverted"])
def test_a_trailer_alone_never_covers_a_merge_with_its_own_changes(tmp_path, trailer):
    path = standalone_repo(tmp_path, "merge-trailer")
    side = commit(path, "side change", {"s.py": lines("s", overrides={2: "S2"})})
    git(path, "checkout", "-q", "-b", "dev", "v1.0")
    main_change = commit(path, "dev change", {"r.py": lines("r", overrides={2: "R2"})})
    commit(path, "evil tree", {"s.py": lines("s", overrides={2: "S2", 9: "EVIL"})})
    evil = git(path, "commit-tree", git(path, "rev-parse", "HEAD^{tree}"), "-p", main_change, "-p", side, "-m", "evil")
    git(path, "update-ref", "refs/remotes/upstream/dev", evil)
    git(path, "checkout", "-q", "--orphan", "main", "v1.0")
    git(path, "commit", "-q", "-m", "fork root")
    if trailer == "empty":
        git(path, "commit", "-q", "--allow-empty", "-m", f"port the merge\n\nUpstream: {evil}")
    else:
        commit(path, f"port the merge\n\nUpstream: {evil}", {"s.py": lines("s", overrides={9: "EVIL"})})
        git(path, "revert", "--no-edit", "HEAD")
    decided = f"## Waived\n\n- `{side}` reason\n- `{main_change}` reason\n"
    result = standalone_gate(path, decided)
    assert result.returncode == 1, result.stdout + result.stderr
    assert evil[:12] in unported_block(result.stdout)
    green = standalone_gate(path, decided + f"- `{evil}` the fork resolves this merge its own way\n")
    assert green.returncode == 0, green.stdout + green.stderr
