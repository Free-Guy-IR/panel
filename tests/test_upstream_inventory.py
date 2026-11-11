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
        {
            "upstream-bridge-waiver.toml": (
                'upstream_version = "0.9.2"\nreason = "fork bridge carries sing-box"\nuntil = 2026-10-20\nsecurity = "no"\n'
            )
        },
    )
    git(path, "checkout", "-q", "-b", "waiver-undated", "main")
    commit(
        path,
        "waive bridge for ever",
        {
            "upstream-bridge-waiver.toml": 'upstream_version = "0.9.2"\nreason = "fork bridge carries sing-box"\nsecurity = "no"\n'
        },
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
                'reason = "0.3.9 only changes docs"\nuntil = 2026-10-20\nsecurity = "no"\n'
            )
        },
    )
    git(path, "checkout", "-q", "-b", "npm-waiver-undated", "main")
    commit(
        path,
        "waive xray kit for ever",
        {
            "upstream-npm-waivers.toml": (
                '[[waiver]]\npackage = "@pasarguard/xray-config-kit"\nupstream_version = "0.3.9"\n'
                'reason = "0.3.9 only changes docs"\nsecurity = "no"\n'
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
                'reason = "nodes still run an older Xray-core"\nuntil = 2026-10-25\nsecurity = "no"\n'
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
        f"- `{shas['u3']}` the fork keeps c as it is security: no, a plain file\n"
        f"- `{shas['u5'][:10]}` the fork bridge has its own floor security: no, a version floor\n"
        f"- `{shas['base']}` older than the base security: no, a stale line\n"
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
        ("- `deadbeef00` a reason security: no\n", "matches no upstream commit"),
        ("- `{short}` a reason security: no\n", "at least 9 hex characters"),
        ("- {u3} no backticks\n", "is not in the form"),
    ],
)
def test_ledger_errors_fail_the_gate(repo, tmp_path, entry, message):
    shas = repo["shas"]
    body = entry.format(u3=shas["u3"], short=shas["u3"][:7])
    waived_rest = "".join(f"- `{shas[k]}` reason security: no\n" for k in ("u5", "f1", "f2"))
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
        (
            "waiver-ok",
            ["--bridge-latest", "0.9.1", "--today", "2026-10-06"],
            0,
            "waived by upstream-bridge-waiver.toml",
        ),
        ("waiver-ok", ["--bridge-latest", "0.9.3"], 1, "is stale: it waives 0.9.2 but upstream is now at 0.9.3"),
        ("waiver-ok", ["--bridge-latest", "0.9.1", "--today", "2026-10-21"], 1, "expired on 2026-10-20; re-check it"),
        ("waiver-ok", ["--bridge-latest", "0.9.1", "--today", "2026-09-15"], 1, "more than 30 days ahead"),
        ("waiver-undated", ["--bridge-latest", "0.9.1", "--today", "2026-10-06"], 1, "has no until date"),
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
    text = (
        f"## Waived\n\n- `{shas['f1']}` pending: tracked feature branch, re-check security: no until: 2026-10-25\n"
        + "".join(f"- `{shas[k]}` reason security: no\n" for k in ("u3", "u5", "f2"))
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
        + "".join(f"- `{shas[key]}` the fork does not take feature x security: no\n" for key in waived)
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
        f"- `{shas['f1']}` the fork does not take feature x security: no\n\n"
        "### Feature x follow-ups\n\n"
        f"- `{shas['f2']}` still waived inside a subsection security: no\n\n"
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
        f"- `{shas['f1']}` the fork does not take feature x security: no\n\n"
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
        f"- `{shas['a']}` UNMERGED-WIP: only on upstream feat-a security: no\n\n"
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
    text = "## Waived\n\n" + "".join(f"- `{shas[k]}` reason security: no\n" for k in ("u3", "u5", "f2"))
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
    plain = subprocess.run(
        args, cwd=repo["path"], env=ENV | {"GITHUB_ACTIONS": "false"}, capture_output=True, text=True, check=False
    )
    actions = subprocess.run(
        args, cwd=repo["path"], env=ENV | {"GITHUB_ACTIONS": "true"}, capture_output=True, text=True, check=False
    )
    assert plain.returncode == actions.returncode == 0, actions.stdout + actions.stderr
    assert "::warning" not in plain.stdout
    annotations = [
        line for line in actions.stdout.splitlines() if line.startswith("::warning title=Owed upstream port::")
    ]
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
    clean = git(
        path, "commit-tree", git(path, "rev-parse", "HEAD^{tree}"), "-p", main_change, "-p", side, "-m", "clean"
    )
    commit(path, "evil tree", {"s.py": lines("s", overrides={2: "S2", 9: "EVIL"})})
    evil = git(path, "commit-tree", git(path, "rev-parse", "HEAD^{tree}"), "-p", main_change, "-p", side, "-m", "evil")
    git(path, "update-ref", "refs/remotes/upstream/dev", evil)
    git(path, "update-ref", "refs/remotes/upstream/clean", clean)
    git(path, "checkout", "-q", "--orphan", "main", "v1.0")
    git(path, "commit", "-q", "-m", "fork root")
    decided = f"## Waived\n\n- `{side}` reason security: no\n- `{main_change}` reason security: no\n"
    red = standalone_gate(path, decided)
    assert red.returncode == 1, red.stdout + red.stderr
    assert "of them merge commits with their own changes: 1" in red.stdout
    assert evil[:12] in unported_block(red.stdout)
    assert clean[:12] not in red.stdout
    green = standalone_gate(path, decided + f"- `{evil}` the fork resolves this merge its own way security: no\n")
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
    assert f"{removal[:12]}: 1 of 1 upstream hunk(s) it changes are not reflected in main" in kept.stdout
    commit(path, f"really port\n\n(cherry picked from commit {removal})", {"r.py": lines("r", 6)})
    ported = standalone_gate(path, "## Waived\n")
    assert ported.returncode == 0, ported.stdout + ported.stderr


def trailer_line(kind, sha):
    return f"(cherry picked from commit {sha})" if kind == "cherry-pick" else f"Upstream: {sha}"


@pytest.mark.parametrize("trailer", ["cherry-pick", "upstream-line"])
@pytest.mark.parametrize("removed", ["lines", "file"])
def test_an_empty_pick_of_a_removal_the_fork_never_had_is_not_a_port(tmp_path, removed, trailer):
    path = standalone_repo(tmp_path, f"never-had-{removed}-{trailer}")
    if removed == "lines":
        removal = commit(path, "drop the legacy path", {"r.py": lines("r", 6)})
    else:
        git(path, "rm", "-q", "s.py")
        git(path, "commit", "-q", "-m", "drop the legacy module")
        removal = git(path, "rev-parse", "HEAD")
    git(path, "update-ref", "refs/remotes/upstream/dev", removal)
    git(path, "checkout", "-q", "--orphan", "main", "v1.0")
    if removed == "lines":
        commit(path, "fork root", {"r.py": lines("r", 6, extra=["fork tail"])})
    else:
        git(path, "rm", "-q", "-f", "s.py")
        git(path, "commit", "-q", "-m", "fork root")
    git(path, "commit", "-q", "--allow-empty", "-m", f"record the port\n\n{trailer_line(trailer, removal)}")
    result = standalone_gate(path, "## Waived\n")
    assert result.returncode == 1, result.stdout + result.stderr
    assert removal[:12] in unported_block(result.stdout)
    first = "r.py: 'removed by no carrier: -r7'" if removed == "lines" else "s.py: 'removed by no carrier: -s1'"
    assert (
        f"{removal[:12]}: 1 of 1 upstream hunk(s) it changes are not reflected in main, for example in {first}"
        in result.stdout
    )


def moved_context_repo(tmp_path, name, fork_text, fixed="R5-checked"):
    path = standalone_repo(tmp_path, name)
    fix = commit(path, "fix: refuse an empty value", {"r.py": lines("r", overrides={5: fixed})})
    later = commit(
        path,
        "refactor: rename the neighbours of the check",
        {"r.py": lines("r", overrides={4: "R4-later", 5: fixed, 6: "R6-later"})},
    )
    git(path, "update-ref", "refs/remotes/upstream/dev", later)
    git(path, "checkout", "-q", "--orphan", "main", "v1.0")
    commit(path, "fork root", {"r.py": fork_text})
    return path, fix, later


def later_waived(later):
    return f"## Waived\n\n- `{later}` reworks the neighbours of the check, decided on its own security: no\n"


def test_an_empty_pick_is_not_a_port_once_upstream_moved_the_context_of_its_hunk(tmp_path):
    path, fix, later = moved_context_repo(tmp_path, "moved-empty", lines("r", overrides={5: "R5-fork"}))
    git(path, "commit", "-q", "--allow-empty", "-m", f"record the port\n\n(cherry picked from commit {fix})")
    result = standalone_gate(path, later_waived(later))
    assert result.returncode == 1, result.stdout + result.stderr
    assert fix[:12] in unported_block(result.stdout)
    assert (
        f"{fix[:12]}: 1 of 1 upstream hunk(s) it changes are not reflected in main, for example in r.py: "
        "'removed by no carrier: -r5'" in result.stdout
    )


def test_a_real_pick_still_counts_once_upstream_moved_the_context_of_its_hunk(tmp_path):
    path, fix, later = moved_context_repo(tmp_path, "moved-real", lines("r"))
    git(path, "cherry-pick", "-x", fix)
    result = standalone_gate(path, later_waived(later))
    assert result.returncode == 0, result.stdout + result.stderr
    assert tally(result.stdout, "ported via cherry-pick -x trailer") == 1


def test_a_pick_carried_by_two_fork_commits_still_counts(tmp_path):
    path = standalone_repo(tmp_path, "split")
    fix = commit(
        path,
        "fix: refuse an empty value",
        {"r.py": lines("r", overrides={5: "R5-checked"}), "s.py": lines("s", extra=["s-check-test"])},
    )
    later = commit(
        path,
        "refactor: rename the neighbours of the check",
        {"r.py": lines("r", overrides={4: "R4-later", 5: "R5-checked", 6: "R6-later"})},
    )
    git(path, "update-ref", "refs/remotes/upstream/dev", later)
    git(path, "checkout", "-q", "--orphan", "main", "v1.0")
    git(path, "commit", "-q", "-m", "fork root")
    commit(path, f"test: the check\n\n(cherry picked from commit {fix})", {"s.py": lines("s", extra=["s-check-test"])})
    commit(path, f"fix: the check\n\n(cherry picked from commit {fix})", {"r.py": lines("r", overrides={5: "R5-checked"})})
    result = standalone_gate(path, later_waived(later))
    assert result.returncode == 0, result.stdout + result.stderr
    assert tally(result.stdout, "ported via cherry-pick -x trailer") == 1


def test_an_adapted_pick_that_replaces_the_forks_own_form_still_counts(tmp_path):
    fixed = "# an empty value is refused before the rest runs\nR5-checked"
    path, fix, later = moved_context_repo(tmp_path, "adapted", lines("r", overrides={5: "R5-fork"}), fixed)
    note = "Adapted: upstream's comment line is left out under the fork's no-comments rule."
    commit(
        path,
        f"fix: take upstream's check\n\n{note}\n\n(cherry picked from commit {fix})",
        {"r.py": lines("r", overrides={5: "R5-checked"})},
    )
    result = standalone_gate(path, later_waived(later))
    assert result.returncode == 0, result.stdout + result.stderr
    assert tally(result.stdout, "ported via cherry-pick -x trailer") == 1


def test_a_pick_of_a_file_deletion_counts_when_its_carrier_deletes_the_forks_own_copy(tmp_path):
    path = standalone_repo(tmp_path, "deleted-own-copy")
    git(path, "rm", "-q", "s.py")
    git(path, "commit", "-q", "-m", "drop the legacy module")
    removal = git(path, "rev-parse", "HEAD")
    git(path, "update-ref", "refs/remotes/upstream/dev", removal)
    git(path, "checkout", "-q", "--orphan", "main", "v1.0")
    commit(path, "fork root", {"s.py": lines("s", overrides={3: "S3-fork"})})
    git(path, "rm", "-q", "s.py")
    git(path, "commit", "-q", "-m", f"drop the legacy module\n\n(cherry picked from commit {removal})")
    result = standalone_gate(path, "## Waived\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert tally(result.stdout, "ported via cherry-pick -x trailer") == 1


def lost_file_repo(tmp_path, layout):
    path = standalone_repo(tmp_path, f"lost-{layout}")
    target = "t.py" if layout == "upstream-renamed" else "s.py"
    if layout == "upstream-renamed":
        git(path, "mv", "s.py", "t.py")
    fix = commit(
        path,
        "fix: tidy both modules",
        {"r.py": lines("r", overrides={2: "R2-tidy"}), target: lines("s", overrides={5: "S5-tidy"})},
    )
    later = commit(
        path,
        "refactor: rename the neighbours of the tidy line",
        {target: lines("s", overrides={4: "S4-later", 5: "S5-tidy", 6: "S6-later"})},
    )
    git(path, "update-ref", "refs/remotes/upstream/dev", later)
    git(path, "checkout", "-q", "--orphan", "main", "v1.0")
    git(path, "commit", "-q", "-m", "fork root")
    carried = {"r.py": lines("r", overrides={2: "R2-tidy"})}
    if layout == "fork-deleted":
        git(path, "rm", "-q", "s.py")
        git(path, "commit", "-q", "-m", "fork drops the module")
    elif layout == "fork-renamed":
        git(path, "mv", "s.py", "s_fork.py")
        git(path, "commit", "-q", "-m", "fork moves the module")
        carried["s_fork.py"] = lines("s", overrides={5: "S5-tidy"})
    commit(path, f"fix: tidy both modules\n\n(cherry picked from commit {fix})", carried)
    return path, fix, later, target


@pytest.mark.parametrize("layout", ["fork-deleted", "fork-renamed", "upstream-renamed"])
def test_a_hunk_in_a_file_the_fork_does_not_have_is_unreflected_until_the_ledger_maps_it(tmp_path, layout):
    path, fix, later, target = lost_file_repo(tmp_path, layout)
    if layout == "upstream-renamed":
        assert (path / "s.py").read_text() == lines("s")
    result = standalone_gate(path, later_waived(later))
    assert result.returncode == 1, result.stdout + result.stderr
    assert fix[:12] in unported_block(result.stdout)
    assert (
        f"{fix[:12]}: 1 of 2 upstream hunk(s) it changes are not reflected in main, for example in {target}: "
        "'the file is not on the head'" in result.stdout
    )
    mapped = later_waived(later) + f"\n## Ported by design\n\n- `{fix}` the fork carries {target} its own way\n"
    covered = standalone_gate(path, mapped)
    assert covered.returncode == 0, covered.stdout + covered.stderr
    assert f"{fix[:12]}: 1 of 2 upstream hunk(s)" in covered.stdout
    assert "covered by its ledger entry" in covered.stdout


@pytest.mark.parametrize("carrier", ["empty", "real"])
def test_a_pick_whose_every_hunk_upstream_reworked_counts_only_when_a_carrier_brings_one(tmp_path, carrier):
    path = standalone_repo(tmp_path, f"reworked-{carrier}")
    added = commit(path, "feat: add a step after r5", {"r.py": lines("r", overrides={5: "r5\nR5-added"})})
    later = commit(
        path,
        "refactor: rename the neighbours of the check",
        {"r.py": lines("r", overrides={4: "R4-later", 5: "r5\nR5-added", 6: "R6-later"})},
    )
    git(path, "update-ref", "refs/remotes/upstream/dev", later)
    git(path, "checkout", "-q", "--orphan", "main", "v1.0")
    git(path, "commit", "-q", "-m", "fork root")
    if carrier == "empty":
        git(path, "commit", "-q", "--allow-empty", "-m", f"record the port\n\n(cherry picked from commit {added})")
    else:
        git(path, "cherry-pick", "-x", added)
    result = standalone_gate(path, later_waived(later))
    if carrier == "real":
        assert result.returncode == 0, result.stdout + result.stderr
        assert tally(result.stdout, "ported via cherry-pick -x trailer") == 1
        return
    assert result.returncode == 1, result.stdout + result.stderr
    assert added[:12] in unported_block(result.stdout)
    assert (
        f"{added[:12]}: none of the hunks it changes can be checked in main, and no fork commit carrying its trailer"
        in result.stdout
    )


def test_an_empty_pick_of_a_comment_only_change_still_counts(tmp_path):
    path = standalone_repo(tmp_path, "comment-only")
    note = commit(path, "docs: explain r5", {"r.py": lines("r", overrides={5: "# r5 stays for old clients\nr5"})})
    git(path, "update-ref", "refs/remotes/upstream/dev", note)
    git(path, "checkout", "-q", "--orphan", "main", "v1.0")
    git(path, "commit", "-q", "-m", "fork root")
    git(path, "commit", "-q", "--allow-empty", "-m", f"record the port\n\n(cherry picked from commit {note})")
    result = standalone_gate(path, "## Waived\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert tally(result.stdout, "ported via cherry-pick -x trailer") == 1


def test_an_orphan_upstream_branch_is_tracked_when_the_base_is_upstream(tmp_path):
    path = standalone_repo(tmp_path, "orphan")
    fix = commit(path, "upstream fix", {"r.py": lines("r", overrides={1: "R1"})})
    git(path, "update-ref", "refs/remotes/upstream/dev", fix)
    orphan = git(path, "commit-tree", git(path, "rev-parse", f"{fix}^{{tree}}"), "-m", "v6 rewrite")
    git(path, "update-ref", "refs/remotes/upstream/v6", orphan)
    git(path, "checkout", "-q", "--orphan", "main", "v1.0")
    git(path, "commit", "-q", "-m", "fork root")
    result = standalone_gate(path, f"## Waived\n\n- `{fix}` reason security: no\n")
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
    result = standalone_gate(path, f"## Waived\n\n- `{guarded}` the fork never calls run security: no\n")
    assert result.returncode == 1, result.stdout + result.stderr
    assert unguarded[:12] in unported_block(result.stdout)


@pytest.mark.parametrize(
    ("reason", "message"),
    [
        ("UNMERGED WIP: only on feat/x", "is waived only while unmerged"),
        ("Pending: feat/x", "is waived only while unmerged"),
        ("**UNMERGED-WIP**: only on feat/x", "an entry is plain text and code spans only"),
    ],
)
def test_a_misspelled_conditional_tag_still_ends_when_upstream_merges(repo, tmp_path, reason, message):
    result = parity(
        repo,
        covering_ledger(tmp_path, repo, reason),
        "--track",
        "refs/upstream-release/merged",
        "--release-ref",
        "refs/upstream-release/merged",
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert message in result.stdout


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
            "## Waived\n\n- `{u3}` reason security: no\n\n## Port queue\n\n- `{u3}` port it security: no until: 2026-10-10\n",
            "is also decided differently",
        ),
        ("## Port queue\n\n- `{u3}` port it until: 2026-10-10\n", "needs exactly one security: yes|no classification"),
        ("## Port queue\n\n- `{u3}` security: no until: 2026-10-10\n", "`{u3}` has no reason"),
        ("## Port queue\n\n- `{u3}` — security: yes. until: 2026-10-10\n", "`{u3}` has no reason"),
        ("## Waived\n\n- `{u3}` until: 2026-10-10\n", "`{u3}` has no reason"),
        ("## Port queue\n\n- `{u3}` (security: no) until: 2026-10-10\n", "`{u3}` has no reason"),
        ("## Port queue\n\n- `{u3}` security: no (until: 2026-10-10)\n", "`{u3}` has no reason"),
        (
            "## Port queue\n\n- `{u3}` **security: no** until: 2026-10-10\n",
            "an entry is plain text and code spans only",
        ),
        ("## Port queue\n\n- `{u3}` security: no! until: 2026-10-10\n", "`{u3}` has no reason"),
        ("## Port queue\n\n- `{u3}` security: no until: 2026-10-10 ?\n", "`{u3}` has no reason"),
        ("## Port queue\n\n- `{u3}` security: no / until: 2026-10-10\n", "`{u3}` has no reason"),
        ("## Port queue\n\n- `{u3}` security: no\u00a0until: 2026-10-10\n", "`{u3}` has no reason"),
        ("## Waived\n\n- `{u3}` (until: 2026-10-10)\n", "`{u3}` has no reason"),
        ("## Port queue\n\n- `{u3}` _security: no_ until: 2026-10-10\n", "an entry is plain text and code spans only"),
        (
            "## Port queue\n\n- `{u3}` port it security: yes security: no until: 2026-10-10\n",
            "needs exactly one security: yes|no classification",
        ),
    ],
)
def test_ledger_forms_that_must_not_count_as_a_decision(repo, tmp_path, text, message):
    shas = repo["shas"]
    rest = "## Waived\n\n" + "".join(f"- `{shas[k]}` reason security: no\n" for k in ("u5", "f1", "f2")) + "\n"
    result = parity(repo, ledger(tmp_path, rest + text.format(u3=shas["u3"])), "--today", "2026-10-06")
    assert result.returncode == 1, result.stdout + result.stderr
    assert message.format(u3=shas["u3"]) in result.stdout


def security_repo(tmp_path):
    path = standalone_repo(tmp_path, "security")
    shas = {
        "auth": commit(
            path,
            "fix(auth): invalidate tokens for replaced admin accounts (#938)",
            {"r.py": lines("r", overrides={1: "R1"})},
        ),
        "plain": commit(path, "chore(deps): bump a library", {"s.py": lines("s", overrides={1: "S1"})}),
        "feature": commit(
            path, "feat(subscription): add a header option", {"r.py": lines("r", overrides={1: "R1", 2: "R2"})}
        ),
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
    green = standalone_gate(
        path, f"## Port queue\n\n- `{ui}` ships a UI tab, not security security: no until: 2026-10-13\n"
    )
    assert green.returncode == 0, green.stdout + green.stderr
    assert tally(green.stdout, OWED_LABEL) == 1
    assert (
        f"`{ui[:12]}` is classified security: no but it names 'session'; re-review the classification" in green.stdout
    )
    assert "is classified security: yes" not in green.stdout


def test_a_security_commit_is_green_once_ported_or_waived(tmp_path):
    path, shas = security_repo(tmp_path)
    queue = (
        "## Port queue\n\n"
        f"- `{shas['plain']}` port the bump security: no until: 2026-10-13\n"
        f"- `{shas['feature']}` port it security: no until: 2026-10-13\n"
    )
    waived = standalone_gate(
        path,
        f"## Waived\n\n- `{shas['auth']}` the fork rejects tokens of replaced admins in its own code {TOKEN_DECISION}\n\n"
        + queue
        + TOKEN_RECORD.format(**shas),
    )
    assert waived.returncode == 0, waived.stdout + waived.stderr
    assert tally(waived.stdout, OWED_LABEL) == 2
    git(path, "cherry-pick", "-x", shas["auth"])
    ported = standalone_gate(
        path, queue + f"- `{shas['auth']}` MUST-PORT: token invalidation security: yes until: 2026-10-13\n"
    )
    assert ported.returncode == 0, ported.stdout + ported.stderr
    assert f"`{shas['auth'][:12]}` is ported now; delete it from the port queue" in ported.stdout


@pytest.mark.parametrize("queue_first", [True, False])
def test_a_port_queue_line_next_to_a_ported_by_design_line_is_stale(repo, tmp_path, queue_first):
    shas = repo["shas"]
    queue = f"## Port queue\n\n- `{shas['u3']}` port it security: no until: 2026-10-13\n\n"
    design = f"## Ported by design\n\n- `{shas['u3']}` adapted by hand in the fork\n\n"
    rest = "## Waived\n\n" + "".join(f"- `{shas[k]}` reason security: no\n" for k in ("u5", "f1", "f2")) + "\n"
    text = rest + (queue + design if queue_first else design + queue)
    result = parity(repo, ledger(tmp_path, text), "--today", "2026-10-06")
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"{shas['u3'][:12]} is both owed in the port queue and ported by design" in result.stdout


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
    (path / "UPSTREAM_SYNC.md").write_text(f"## Waived\n\n- `{feature}` feature branch work, not merged security: no\n")
    result = _gate(path)
    assert result.returncode == 1
    assert f"upstream head upstream/feature at {feature[:12]} is not recorded" in result.stdout
    assert (
        "record the reviewed heads by re-running with --write-refs-snapshot, which writes .upstream-refs"
        in result.stdout
    )
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
    (path / "UPSTREAM_SYNC.md").write_text(f"## Waived\n\n- `{feature}` deleted upstream without merge security: no\n")
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
        f"- `{shas['f1']}` {f1_reason} security: no\n"
        f"- `{shas['u3']}` the fork keeps c as it is security: no\n"
        f"- `{shas['u5']}` the fork bridge has its own floor security: no\n",
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
        (
            "npm-waiver-ok",
            ["--npm-latest", "@pasarguard/xray-config-kit=0.3.9", "--today", "2026-10-06"],
            0,
            "waived by upstream-npm-waivers",
        ),
        (
            "npm-waiver-undated",
            ["--npm-latest", "@pasarguard/xray-config-kit=0.3.9", "--today", "2026-10-06"],
            1,
            "has no until date; a waiver needs a dated re-check",
        ),
        (
            "npm-waiver-dated",
            ["--npm-latest", "@pasarguard/xray-config-kit=0.3.9", "--today", "2026-09-20"],
            1,
            "more than 30 days ahead",
        ),
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
    text = "## Waived\n\n" + "".join(f"- `{shas[k]}` reason security: no\n" for k in ("u3", "u5", "f1", "f2"))
    unlisted = parity(repo, ledger(tmp_path, text, adapted=False))
    assert unlisted.returncode == 1
    block = unported_block(unlisted.stdout)
    assert {sha for sha in shas.values() if sha[:12] in block} == {shas["u2"], shas["u4"]}
    assert f"{shas['u2'][:12]}: 2 of 2 upstream hunk(s) it changes are not reflected in main" in unlisted.stdout
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


TOKEN_DECISION = "security: yes owner-decision: 2026-10-01-fork-token-check"
TOKEN_RECORD = (
    "\n## Owner decisions\n\n- `2026-10-01-fork-token-check` the fork rejects tokens of replaced admins in its own "
    "code, covers {auth} until: 2026-10-31\n"
)
CRAFTED_WAIVED = (
    f"## Waived\n\n- `{{auth}}` the fork rejects tokens of replaced admins in its own code {TOKEN_DECISION}\n"
)
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
    "waived-in-continuation": (
        "",
        "  wrapped reason of the entry above\n## Waived\n- `{plain}` crafted\n",
        REPEATED_SECTION,
    ),
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
        "- `{plain}` crafted waiver security: no\n",
        "- `{plain}` bump the library security: yes until: 2026-10-13\n",
        "is also decided differently",
    ),
}


def crafted_ledger(shas, in_waived, in_queue):
    return (CRAFTED_WAIVED + in_waived + CRAFTED_QUEUE + in_queue + TOKEN_RECORD).format(**shas)


def test_the_crafted_ledger_baseline_is_green_once_the_commit_is_really_decided(tmp_path):
    path, shas = security_repo(tmp_path)
    decided = crafted_ledger(
        shas, "", "\n## Ported by design\n\n- `{plain}` the fork carries the bump in its own lock\n"
    )
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
    head = f"## Ported by design\n\n- `{shas['auth']}` the fork rejects tokens of replaced admins in its own code security: yes\n\n"
    qa = f"- `{a}` until: 2026-10-10 security: no: small tweak\n"
    qb = f"- `{b}` until: 2026-10-10 security: yes: bump fix, port now\n"
    wa = f"- `{a}` waived: fork does not use it security: no\n"
    return {
        "control_queue": (head + "## Port queue\n\n" + qa + qb, "red"),
        "hash_no_space": (head + "## Port queue\n\n" + qa + "#waived\n" + qb, NOT_A_HEADING),
        "setext_port_queue": (head + "## Waived\n\n" + wa + "\nPort queue\n----------\n\n" + qb, "setext heading"),
        "indented_1sp_atx": (head + "## Waived\n\n" + wa + "\n ## Port queue\n\n" + qb, "heading is indented"),
        "blockquote_heading": (head + "## Waived\n\n" + wa + "\n> ## Port queue\n\n" + qb, UNSUPPORTED_BLOCK),
        "fenced_heading": (head + "## Port queue\n\n" + qa + "\n~~~\n## Waived\n~~~\n\n" + qb, UNSUPPORTED_BLOCK),
        "backtick_fence_heading": (
            head + "## Port queue\n\n" + qa + "\n```\n## Waived\n```\n\n" + qb,
            UNSUPPORTED_BLOCK,
        ),
        "html_comment_in_code_span": (
            head
            + "## Waived\n\n"
            + f"- `{a}` waived: it adds `<!--` markers to templates security: no\n"
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
        "script_block_heading": (
            head + "## Port queue\n\n" + qa + "\n<script>\n## Waived\n</script>\n\n" + qb,
            HTML_IN_LEDGER,
        ),
        "style_block_heading": (
            head + "## Port queue\n\n" + qa + "\n<style>\n## Waived\n</style>\n\n" + qb,
            HTML_IN_LEDGER,
        ),
        "pre_block_heading": (head + "## Port queue\n\n" + qa + "\n<pre>\n## Waived\n</pre>\n\n" + qb, HTML_IN_LEDGER),
        "nbsp_list_marker": (head + "## Waived\n\n" + wa + "-\u00a0`" + b + "` waived: looks like prose\n", "unported"),
        "crlf_control": ((head + "## Port queue\n\n" + qa + qb).replace("\n", "\r\n"), "red"),
        "trailing_hash_close": (
            head + "## Waived\n\n" + wa + "\n## Port queue\n\n" + qb + "## Waived ##\n" + qb,
            REPEATED_SECTION,
        ),
        "tab_after_hashes": (
            head + "## Waived\n\n" + wa + "\n## Port queue\n\n" + qb + "##\tWaived\n" + qb,
            REPEATED_SECTION,
        ),
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
    [
        "UPSTREAM_SYNC.md",
        *sorted(
            str(p.relative_to(SCRIPT.parent.parent)) for p in (SCRIPT.parent.parent / "upstream-forks").glob("*.md")
        ),
    ],
)
def test_the_real_ledgers_have_no_structural_errors(ledger):
    sys.path.insert(0, str(SCRIPT.parent))
    import upstream_inventory

    root = SCRIPT.parent.parent
    items, errors = upstream_inventory.ledger_items(ledger, (root / ledger).read_text())
    assert errors == []
    if ledger == "UPSTREAM_SYNC.md":
        assert len(items) > 50
    _, entry_errors = upstream_inventory.parse_sync_ledger(str(root / ledger))
    assert [error for error in entry_errors if "security" in error or "owner decision" in error] == []


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
    decided = f"## Waived\n\n- `{side}` reason security: no\n- `{main_change}` reason security: no\n"
    result = standalone_gate(path, decided)
    assert result.returncode == 1, result.stdout + result.stderr
    assert evil[:12] in unported_block(result.stdout)
    green = standalone_gate(path, decided + f"- `{evil}` the fork resolves this merge its own way security: no\n")
    assert green.returncode == 0, green.stdout + green.stderr


@pytest.mark.parametrize(
    ("until", "message"),
    [
        ("until 2026/10/20", "has an until date the gate cannot read"),
        ("Until: 2026.10.20", "has an until date the gate cannot read"),
        ("until: **2026-10-20**", "an entry is plain text and code spans only"),
        ("until: _2026-10-20_", "an entry is plain text and code spans only"),
    ],
)
@pytest.mark.parametrize("section", ["Waived", "Port queue"])
def test_an_until_date_the_gate_cannot_read_fails_instead_of_becoming_permanent(tmp_path, until, message, section):
    path, shas = security_repo(tmp_path)
    flag = " security: no" if section == "Port queue" else ""
    if section == "Waived":
        text = crafted_ledger(shas, f"- `{shas['plain']}` the fork pins its own copy security: no {until}\n", "")
    else:
        text = crafted_ledger(shas, "", f"- `{shas['plain']}` bump the library{flag} {until}\n")
    result = standalone_gate(path, text)
    assert result.returncode == 1, result.stdout + result.stderr
    assert message in result.stdout


@pytest.mark.parametrize("section", ["Waived", "Port queue"])
def test_an_until_date_in_a_code_span_is_honoured_as_a_real_deadline(tmp_path, section):
    path, shas = security_repo(tmp_path)
    if section == "Waived":
        text = crafted_ledger(
            shas, f"- `{shas['plain']}` the fork pins its own copy security: no until `2026-10-20`\n", ""
        )
    else:
        text = crafted_ledger(shas, "", f"- `{shas['plain']}` bump the library security: no until `2026-10-20`\n")
    before = standalone_gate(path, text)
    assert before.returncode == 0, before.stdout + before.stderr
    after = standalone_gate(path, text, "--today", "2026-10-21")
    assert after.returncode == 1, after.stdout + after.stderr
    assert "was decided only until 2026-10-20" in after.stdout


def test_a_readable_until_date_still_passes(tmp_path):
    path, shas = security_repo(tmp_path)
    text = crafted_ledger(shas, f"- `{shas['plain']}` the fork pins its own copy security: no until: 2026-10-20\n", "")
    assert standalone_gate(path, text).returncode == 0


HELD_LINE = "\n## Held for an upstream release\n\n- `{plain}` bump the library, waiting for upstream to release it security: yes\n"


def held_repo(tmp_path):
    path, shas = security_repo(tmp_path)
    git(path, "update-ref", "refs/upstream-tags/v1.0", git(path, "rev-parse", "v1.0"))
    return path, shas


def test_a_held_dev_only_security_commit_keeps_the_gate_green(tmp_path):
    path, shas = held_repo(tmp_path)
    result = standalone_gate(path, crafted_ledger(shas, "", HELD_LINE))
    assert result.returncode == 0, result.stdout + result.stderr
    assert tally(result.stdout, "held until an upstream release (ledger)") == 1
    assert "is classified security: yes" not in result.stdout


def test_a_held_commit_fails_the_gate_the_moment_an_upstream_release_contains_it(tmp_path):
    path, shas = held_repo(tmp_path)
    ledger_text = crafted_ledger(shas, "", HELD_LINE)
    assert standalone_gate(path, ledger_text).returncode == 0
    git(path, "update-ref", "refs/upstream-tags/v1.1", shas["feature"])
    released = standalone_gate(path, ledger_text)
    assert released.returncode == 1, released.stdout + released.stderr
    assert f"`{shas['plain'][:12]}` is held for an upstream release, but upstream released it in v1.1" in (
        released.stdout
    )
    assert shas["plain"][:12] in unported_block(released.stdout)
    waived = ledger_text.replace(
        "## Waived\n\n", f"## Waived\n\n- `{shas['plain']}` the fork pins its own copy of the library security: no\n"
    ).replace(HELD_LINE.format(**shas), "")
    assert standalone_gate(path, waived).returncode == 0
    git(path, "cherry-pick", "-x", shas["plain"])
    ported = standalone_gate(path, ledger_text)
    assert ported.returncode == 0, ported.stdout + ported.stderr
    assert "is ported now; delete it from the held list" in ported.stdout


def test_a_held_entry_for_a_commit_that_is_already_released_is_a_ledger_error(tmp_path):
    path, shas = held_repo(tmp_path)
    git(path, "update-ref", "refs/upstream-tags/v1.1", shas["plain"])
    result = standalone_gate(path, crafted_ledger(shas, "", HELD_LINE))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "is held for an upstream release, but upstream released it in v1.1" in result.stdout


def test_a_released_copy_of_a_held_commit_needs_its_own_decision(tmp_path):
    path, shas = held_repo(tmp_path)
    git(path, "checkout", "-q", "-b", "point", "v1.0")
    git(path, "cherry-pick", shas["plain"])
    git(path, "commit", "-q", "--amend", "-m", "chore(deps): the same bump on a point release")
    copy = git(path, "rev-parse", "HEAD")
    git(path, "update-ref", "refs/upstream-tags/v1.0.1", copy)
    git(path, "checkout", "-q", "main")
    ledger_text = crafted_ledger(shas, "", HELD_LINE)
    assert standalone_gate(path, ledger_text).returncode == 0
    result = standalone_gate(path, ledger_text, "--track", "refs/upstream-tags/*")
    assert result.returncode == 1, result.stdout + result.stderr
    assert copy[:12] in unported_block(result.stdout)
    assert "which is held for an upstream release, so the released copy needs its own decision" in result.stdout


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("- `{plain}` waiting for the release security: no until: 2026-10-20\n", "which has no until date"),
        ("- `{plain}` waiting for the release\n", "needs exactly one security: yes|no classification"),
        ("- `{plain}` security: yes\n", "has no reason"),
    ],
)
def test_a_held_entry_needs_a_reason_and_a_classification_and_no_deadline(tmp_path, line, message):
    path, shas = held_repo(tmp_path)
    text = crafted_ledger(shas, "", "\n## Held for an upstream release\n\n" + line)
    result = standalone_gate(path, text)
    assert result.returncode == 1, result.stdout + result.stderr
    assert message in result.stdout


def test_a_held_entry_without_any_release_tag_fails_closed(tmp_path):
    path, shas = security_repo(tmp_path)
    result = standalone_gate(path, crafted_ledger(shas, "", HELD_LINE))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "need at least one existing upstream release tag" in result.stdout


FORMATTED_ENTRY = "an entry is plain text and code spans only"
HIDDEN_QUEUE_LINES = {
    "escaped-yes-title-no": '- `{plain}` security\\: yes: bump fix, [details](#port-queue "security: no") until: 2026-10-13\n',
    "entity-yes-title-no": '- `{plain}` security&#58; yes: bump fix, [details](#port-queue "security: no") until: 2026-10-13\n',
    "escaped-yes-image-alt-no": "- `{plain}` security\\: yes: bump fix ![security: no](p.png) until: 2026-10-13\n",
    "escaped-yes-href-no": "- `{plain}` security\\: yes: bump fix, [details](security:no) until: 2026-10-13\n",
    "entity-until-title-until": '- `{plain}` security: no tweak until 2026&#45;10&#45;10 [n](#q "until: 2026-11-01")\n',
    "emphasis-flag": "- `{plain}` bump fix *security: no* until: 2026-10-13\n",
}
HIDDEN_WAIVED_UNTIL = {
    "entity-dash": "- `{plain}` waived until 2026&#45;10&#45;20: re-check then\n",
    "backslash-dash": "- `{plain}` waived until 2026\\-10\\-20: re-check then\n",
    "emphasis-day": "- `{plain}` waived until 2026-10-*20*: re-check then\n",
    "strong-month": "- `{plain}` waived until 2026-**10**-20: re-check then\n",
    "entity-digit": "- `{plain}` waived until &#50;026-10-20: re-check then\n",
}


@pytest.mark.parametrize("case", sorted(HIDDEN_QUEUE_LINES))
def test_a_queue_line_whose_source_hides_what_it_renders_is_a_ledger_error(tmp_path, case):
    path, shas = security_repo(tmp_path)
    result = standalone_gate(path, crafted_ledger(shas, "", HIDDEN_QUEUE_LINES[case]))
    assert result.returncode == 1, result.stdout + result.stderr
    assert FORMATTED_ENTRY in result.stdout
    assert shas["plain"][:12] in unported_block(result.stdout)


@pytest.mark.parametrize("case", sorted(HIDDEN_WAIVED_UNTIL))
def test_a_waiver_date_written_with_markup_is_a_ledger_error_not_a_permanent_waiver(tmp_path, case):
    path, shas = security_repo(tmp_path)
    result = standalone_gate(path, crafted_ledger(shas, HIDDEN_WAIVED_UNTIL[case], ""))
    assert result.returncode == 1, result.stdout + result.stderr
    assert FORMATTED_ENTRY in result.stdout


def test_a_code_span_inside_an_until_date_is_read_as_the_reviewer_sees_it(tmp_path):
    path, shas = security_repo(tmp_path)
    text = crafted_ledger(shas, "- `{plain}` waived security: no until `2026`-10-20: re-check then\n", "")
    assert standalone_gate(path, text).returncode == 0
    expired = standalone_gate(path, text, "--today", "2026-10-21")
    assert expired.returncode == 1, expired.stdout + expired.stderr
    assert "was decided only until 2026-10-20" in expired.stdout


def test_a_heading_with_markup_is_a_ledger_error(tmp_path):
    path, shas = security_repo(tmp_path)
    text = crafted_ledger(shas, "", "") + "\n## *Ported by design*\n\n- `{plain}` the fork carries it\n".format(**shas)
    result = standalone_gate(path, text)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "a ledger heading is plain text" in result.stdout


def merge_repo(tmp_path):
    path = standalone_repo(tmp_path, "merge")
    shas = {
        "fix": commit(path, "fix a check", {"r.py": lines("r", overrides={1: "R1"})}),
        "bump": commit(path, "bump a library", {"s.py": lines("s", overrides={1: "S1"})}),
    }
    git(path, "update-ref", "refs/remotes/upstream/dev", shas["bump"])
    git(path, "tag", "v1.1", shas["bump"])
    git(path, "checkout", "-q", "-b", "main", "v1.0")
    commit(path, "fork root change", {"t.py": lines("t")})
    git(path, "merge", "-q", "--no-ff", "-m", "merge upstream v1.1", "v1.1")
    return path, shas


def test_an_upstream_release_merged_into_the_head_counts_as_ported(tmp_path):
    path, _ = merge_repo(tmp_path)
    result = standalone_gate(path, "## Waived\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert tally(result.stdout, "merged into the head, every line present") == 2


def test_a_merged_upstream_commit_whose_lines_the_fork_dropped_is_unported(tmp_path):
    path, shas = merge_repo(tmp_path)
    commit(path, "fork keeps its own s.py", {"s.py": lines("s")})
    result = standalone_gate(path, "## Waived\n")
    assert result.returncode == 1, result.stdout + result.stderr
    assert shas["bump"][:12] in unported_block(result.stdout)
    assert f"{shas['bump'][:12]}: merged into main, but 1 of 1 hunk(s)" in result.stdout
    assert tally(result.stdout, "merged into the head, every line present") == 1
    waived = standalone_gate(
        path, f"## Waived\n\n- `{shas['bump']}` the fork pins its own copy of the library security: no\n"
    )
    assert waived.returncode == 0, waived.stdout + waived.stderr


def test_a_grandfathered_baseline_is_trusted_as_it_is(tmp_path):
    path, _ = merge_repo(tmp_path)
    result = standalone_gate(path, "## Waived\n", "--grandfathered-base", "v1.0")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "is not grandfathered" not in result.stdout
    assert tally(result.stdout, "merged into the head, every line present") == 2


def held_gate(path, shas, *extra):
    return standalone_gate(
        path,
        crafted_ledger(shas, "", HELD_LINE),
        "--track",
        "refs/upstream-tags/*",
        "--refs-snapshot",
        ".upstream-refs",
        *extra,
    )


@pytest.mark.parametrize("change", ["delete", "move"])
def test_a_release_once_recorded_keeps_its_held_commits_released(tmp_path, change):
    path, shas = held_repo(tmp_path)
    (path / ".upstream-refs").write_text("")
    git(path, "update-ref", "refs/upstream-tags/v1.1", shas["feature"])
    assert held_gate(path, shas, "--write-refs-snapshot").returncode == 1
    assert f"{shas['feature']} v1.1" in (path / ".upstream-refs").read_text()
    if change == "delete":
        git(path, "update-ref", "-d", "refs/upstream-tags/v1.1")
    else:
        git(path, "update-ref", "refs/upstream-tags/v1.1", git(path, "rev-parse", "v1.0"))
    result = held_gate(path, shas, "--write-refs-snapshot")
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"`{shas['plain'][:12]}` is held for an upstream release, but upstream released it in v1.1 (recorded in" in (
        result.stdout
    )


def test_a_narrowed_held_release_set_is_refused(tmp_path):
    path, shas = held_repo(tmp_path)
    git(path, "update-ref", "refs/upstream-tags/v1.1", shas["feature"])
    result = standalone_gate(path, crafted_ledger(shas, "", HELD_LINE), "--held-release-ref", "refs/upstream-tags/v1.0")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "--held-release-ref leaves out 1 upstream release tag(s), for example v1.1" in result.stdout


@pytest.mark.parametrize("tag", ["v1.1-rc.1", "v1.1.0-beta-2", "v1.1rc1"])
def test_a_prerelease_tag_keeps_a_commit_held_and_the_final_tag_releases_it(tmp_path, tag):
    path, shas = held_repo(tmp_path)
    git(path, "update-ref", f"refs/upstream-tags/{tag}", shas["feature"])
    held = standalone_gate(path, crafted_ledger(shas, "", HELD_LINE))
    assert held.returncode == 0, held.stdout + held.stderr
    assert f"`{shas['plain'][:12]}` is in upstream pre-release {tag} only; it stays held" in held.stdout
    git(path, "update-ref", "refs/upstream-tags/v1.1", shas["feature"])
    released = standalone_gate(path, crafted_ledger(shas, "", HELD_LINE))
    assert released.returncode == 1, released.stdout + released.stderr
    assert "but upstream released it in v1.1" in released.stdout


def test_a_waiver_until_written_as_a_date_time_is_refused_without_a_traceback(repo, tmp_path):
    path = repo["path"]
    git(path, "checkout", "-q", "-b", "waiver-datetime", "main")
    commit(
        path,
        "waive with a date-time",
        {
            "upstream-bridge-waiver.toml": (
                'upstream_version = "0.9.2"\nreason = "fork bridge carries sing-box"\nuntil = 2026-10-20T00:00:00\n'
            ),
            "upstream-npm-waivers.toml": (
                '[[waiver]]\npackage = "@pasarguard/xray-config-kit"\nupstream_version = "0.3.9"\n'
                'reason = "0.3.9 only changes docs"\nuntil = 2026-10-20T00:00:00\n'
            ),
        },
    )
    git(path, "checkout", "-q", "main")
    bridge = run(
        path, "--head", "waiver-datetime", "--check-bridge", "--bridge-latest", "0.9.1", "--today", "2026-10-06"
    )
    assert bridge.returncode == 1, bridge.stdout + bridge.stderr
    assert "is not a YYYY-MM-DD date" in bridge.stdout and "Traceback" not in bridge.stderr
    npm = run(
        path,
        "--head",
        "waiver-datetime",
        "--check-npm",
        "--npm-latest",
        "@pasarguard/xray-config-kit=0.3.9",
        "--today",
        "2026-10-06",
    )
    assert npm.returncode != 0, npm.stdout + npm.stderr
    assert "is a date-time; write a YYYY-MM-DD date" in npm.stdout + npm.stderr and "Traceback" not in npm.stderr


BASELINE_FROM_FLOOR = "the baseline v1.1 is not grandfathered, so the gate checks every upstream commit from v1.0"


def floor_repo(tmp_path, name):
    path = standalone_repo(tmp_path, name)
    shas = {
        "fix": commit(path, "fix a check", {"r.py": lines("r", overrides={1: "CHECK_TOKEN_STRICT"})}),
        "bump": commit(path, "bump a library", {"s.py": lines("s", overrides={1: "S1"})}),
    }
    git(path, "update-ref", "refs/remotes/upstream/dev", shas["bump"])
    git(path, "tag", "v1.1", shas["bump"])
    git(path, "checkout", "-q", "-b", "main", "v1.0")
    commit(path, "fork feature", {"t.py": lines("t")})
    return path, shas


def bumped_gate(path, *extra):
    return standalone_gate(path, "## Waived\n", "--base", "v1.1", "--grandfathered-base", "v1.0", *extra)


def merge_with_ours(path, shas):
    git(path, "merge", "-q", "-s", "ours", "--no-edit", "v1.1")


def merge_then_revert(path, shas):
    git(path, "merge", "-q", "--no-ff", "--no-edit", "v1.1")
    git(path, "revert", "--no-edit", "-m", "1", "HEAD")


def squash(path, shas, files):
    git(path, "checkout", "-q", "v1.1", "--", *files)
    trailers = "".join(f"Upstream: {shas[key]}\n" for key in ("fix", "bump"))
    git(path, "commit", "-q", "-m", f"sync upstream v1.1\n\n{trailers}")


@pytest.mark.parametrize("hide", [merge_with_ours, merge_then_revert])
def test_a_baseline_bump_over_a_merge_that_dropped_upstream_lines_fails(tmp_path, hide):
    path, shas = floor_repo(tmp_path, hide.__name__)
    hide(path, shas)
    assert "CHECK_TOKEN_STRICT" not in git(path, "show", "main:r.py")
    result = bumped_gate(path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert BASELINE_FROM_FLOOR in result.stdout
    assert {shas["fix"][:12], shas["bump"][:12]} <= set(unported_block(result.stdout).split())


def test_a_squashed_sync_that_dropped_one_upstream_hunk_fails_after_a_baseline_bump(tmp_path):
    path, shas = floor_repo(tmp_path, "squash-dropped")
    squash(path, shas, ["r.py"])
    result = bumped_gate(path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert BASELINE_FROM_FLOOR in result.stdout
    unported = unported_block(result.stdout)
    assert shas["bump"][:12] in unported and shas["fix"][:12] not in unported


@pytest.mark.parametrize("sync", ["squash", "merge"])
def test_a_baseline_bump_is_green_when_every_upstream_line_is_in_the_fork(tmp_path, sync):
    path, shas = floor_repo(tmp_path, f"proven-{sync}")
    if sync == "squash":
        squash(path, shas, ["r.py", "s.py"])
    else:
        git(path, "merge", "-q", "--no-ff", "--no-edit", "v1.1")
    result = bumped_gate(path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert BASELINE_FROM_FLOOR in result.stdout
    assert "all 2 upstream commit(s) are ported" in result.stdout


def test_a_baseline_unrelated_to_every_grandfathered_base_is_refused(tmp_path):
    path, _ = floor_repo(tmp_path, "unrelated-base")
    git(path, "checkout", "-q", "--orphan", "elsewhere")
    commit(path, "unrelated root", {"u.py": lines("u")})
    git(path, "tag", "v9.0")
    git(path, "checkout", "-q", "main")
    result = standalone_gate(path, "## Waived\n", "--base", "v9.0", "--grandfathered-base", "v1.0")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "the baseline v9.0" in result.stderr and "descends from no --grandfathered-base" in result.stderr


def test_the_workflow_checks_the_baseline_from_the_grandfathered_release():
    workflow = (SCRIPT.parent.parent / ".github" / "workflows" / "upstream-inventory.yml").read_text()
    gate = workflow.split("name: Upstream parity gate", 1)[1].split("- name:", 1)[0]
    assert "--grandfathered-base b56ffe369f542152c52c69733205baeaf3f6e4cd" in gate
    assert "--require-base-ancestor" not in workflow


def test_the_workflow_scopes_blocking_commits_to_upstream_main_and_release_tags():
    workflow = (SCRIPT.parent.parent / ".github" / "workflows" / "upstream-inventory.yml").read_text()
    gate = workflow.split("name: Upstream parity gate", 1)[1].split("- name:", 1)[0]
    assert "--release-scope-ref refs/remotes/upstream/main" in gate
    assert "--release-scope-ref 'refs/upstream-tags/*'" in gate
    assert "--release-scope-ref refs/remotes/upstream/dev" not in workflow


CVE_SUBJECT = "fix(auth): reject forged session tokens (CVE-2026-12345)"


def classified_repo(tmp_path, name, subject=CVE_SUBJECT):
    path = standalone_repo(tmp_path, name)
    shas = {
        "sec": commit(path, subject, {"r.py": lines("r", overrides={1: "R1"})}),
        "plain": commit(path, "chore(deps): bump a library", {"s.py": lines("s", overrides={1: "S1"})}),
    }
    git(path, "update-ref", "refs/remotes/upstream/dev", shas["plain"])
    git(path, "update-ref", "refs/upstream-tags/v1.0", git(path, "rev-parse", "v1.0"))
    git(path, "checkout", "-q", "--orphan", "main", "v1.0")
    git(path, "commit", "-q", "-m", "fork root")
    return path, shas


def classified_ledger(shas, section, sec_reason):
    plain = f"- `{shas['plain']}` the fork pins its own copy security: no\n"
    if section == "Waived":
        return f"## Waived\n\n- `{shas['sec']}` {sec_reason}\n{plain}"
    return f"## Waived\n\n{plain}\n## {section}\n\n- `{shas['sec']}` {sec_reason}\n"


def test_a_waived_entry_without_a_security_classification_fails(tmp_path):
    path, shas = classified_repo(tmp_path, "waived-unclassified", "feat(ui): add a column")
    result = standalone_gate(path, classified_ledger(shas, "Waived", "the fork has its own column"))
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"`{shas['sec']}` waived needs exactly one security: yes|no classification" in result.stdout


@pytest.mark.parametrize(
    ("decision", "code", "message"),
    [
        ("", 1, "a waived security fix needs the owner's decision, written as owner-decision: YYYY-MM-DD-<name>"),
        ("owner-decision: 2026-10-05", 1, "a waived security fix needs the owner's decision"),
        ("owner-decision: tomorrow-maybe", 1, "a waived security fix needs the owner's decision"),
        ("owner-decision: 2026-02-30-hold", 1, "names an owner decision with an invalid date"),
        ("owner-decision: 2026-10-07-hold", 1, "names an owner decision dated 2026-10-07, after today"),
        ("owner-decision: 2026-10-05-fork-token-check", 0, "Upstream parity gate OK"),
    ],
)
def test_a_waived_security_fix_needs_a_named_owner_decision(tmp_path, decision, code, message):
    path, shas = classified_repo(tmp_path, "waived-security")
    record = decision_list(
        "2026-10-05-fork-token-check the fork keeps its own check, covers {sec} until: 2026-11-04", shas=shas
    )
    result = standalone_gate(
        path,
        classified_ledger(shas, "Waived", f"the fork rejects forged tokens itself security: yes {decision}") + record,
    )
    assert result.returncode == code, result.stdout + result.stderr
    assert message in result.stdout


@pytest.mark.parametrize(
    ("section", "flag"),
    [
        ("Waived", "security: no"),
        ("Ported by design", "security: no"),
        ("Port queue", "security: no until: 2026-10-13"),
        ("Held for an upstream release", "security: no"),
    ],
)
def test_a_cve_fix_classified_security_no_fails_in_every_section(tmp_path, section, flag):
    path, shas = classified_repo(tmp_path, f"cve-{section.split()[0].lower()}")
    result = standalone_gate(path, classified_ledger(shas, section, f"the fork has its own check {flag}"))
    assert result.returncode == 1, result.stdout + result.stderr
    assert (
        f"`{shas['sec'][:12]}` is classified security: no, but it names 'CVE-2026-12345', which marks a security fix"
        in result.stdout
    )


def test_a_cve_named_only_in_the_reason_also_needs_security_yes(tmp_path):
    path, shas = classified_repo(tmp_path, "cve-in-reason", "chore: tidy the token module")
    result = standalone_gate(
        path, classified_ledger(shas, "Waived", "upstream tidies around GHSA-abcd-efgh-ijkl, not needed security: no")
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "it names 'GHSA-abcd-efgh-ijkl', which marks a security fix" in result.stdout


@pytest.mark.parametrize(
    ("reason", "code", "message"),
    [
        (
            "the fork has its own token check",
            1,
            "is ported by design but it names 'auth'; classify it security: yes|no",
        ),
        (
            "the fork has its own token check security: yes",
            1,
            "is ported by design as a security fix, but its reason names no fork commit on main",
        ),
        ("the fork has its own token check security: no", 0, "is classified security: no but it names 'auth'"),
    ],
)
def test_a_ported_by_design_entry_for_a_security_looking_commit_needs_a_classification(tmp_path, reason, code, message):
    path, shas = classified_repo(tmp_path, "pbd-hint", "fix(auth): drop stale admin sessions")
    result = standalone_gate(path, classified_ledger(shas, "Ported by design", reason))
    assert result.returncode == code, result.stdout + result.stderr
    assert message in result.stdout


def test_a_ported_by_design_entry_without_any_security_hint_needs_no_classification(tmp_path):
    path, shas = classified_repo(tmp_path, "pbd-plain", "feat(ui): add a column")
    result = standalone_gate(path, classified_ledger(shas, "Ported by design", "the fork has its own column"))
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_waived_security_no_entry_with_a_keyword_needs_security_yes(tmp_path):
    path, shas = classified_repo(tmp_path, "waived-keyword", "feat(ui): remember the last open session tab")
    result = standalone_gate(path, classified_ledger(shas, "Waived", "the fork has no such tab security: no"))
    assert result.returncode == 1, result.stdout + result.stderr
    assert (
        f"`{shas['sec'][:12]}` is waived as security: no, but it names 'session'; a waiver of a security-looking"
        in (result.stdout)
    )


BRIDGE_WAIVER = 'upstream_version = "0.9.2"\nuntil = 2026-10-20\n'
NPM_WAIVER = '[[waiver]]\npackage = "@pasarguard/xray-config-kit"\nupstream_version = "0.3.9"\nuntil = 2026-10-20\n'
BRIDGE_RECORD = (
    "## Owner decisions\n\n- `2026-10-05-bridge-hold` the fork bridge stays one more release, covers "
    "`pasarguard-node-bridge@0.9.2` until: 2026-11-04\n"
)
NPM_RECORD = (
    "## Owner decisions\n\n- `2026-10-05-kit-hold` the kit waits for the dashboard rework, covers "
    "`@pasarguard/xray-config-kit@0.3.9` until: 2026-11-04\n"
)


@pytest.mark.parametrize(
    ("name", "fields", "code", "message"),
    [
        ("bridge-unclassified", 'reason = "fork bridge carries sing-box"\n', 1, 'needs security = "yes" or "no"'),
        (
            "bridge-bool",
            'reason = "fork bridge carries sing-box"\nsecurity = false\n',
            1,
            'needs security = "yes" or "no"',
        ),
        (
            "bridge-sec-undecided",
            'reason = "0.9.2 closes an auth bypass; we defer it"\nsecurity = "yes"\n',
            1,
            'a security waiver needs the owner\'s decision, written as owner_decision = "YYYY-MM-DD-<name>"',
        ),
        (
            "bridge-sec-decided",
            (
                'reason = "0.9.2 closes an auth bypass; we defer it"\nsecurity = "yes"\n'
                'owner_decision = "2026-10-05-bridge-hold"\n'
            ),
            0,
            "waived by upstream-bridge-waiver.toml",
        ),
        (
            "bridge-cve-no",
            'reason = "0.9.2 fixes CVE-2026-99999, an authentication bypass; we defer it"\nsecurity = "no"\n',
            1,
            "is classified security no, but it names 'CVE-2026-99999', which marks a security fix",
        ),
        (
            "bridge-classified",
            'reason = "fork bridge carries sing-box"\nsecurity = "no"\n',
            0,
            "waived by upstream-bridge",
        ),
    ],
)
def test_a_bridge_waiver_needs_a_security_classification(repo, name, fields, code, message):
    path = repo["path"]
    git(path, "checkout", "-q", "-b", name, "main")
    files = {"upstream-bridge-waiver.toml": BRIDGE_WAIVER + fields}
    if name.endswith("-decided"):
        files["UPSTREAM_SYNC.md"] = BRIDGE_RECORD
    commit(path, f"waive bridge: {name}", files)
    git(path, "checkout", "-q", "main")
    result = run(path, "--head", name, "--check-bridge", "--bridge-latest", "0.9.1", "--today", "2026-10-06")
    assert result.returncode == code, result.stdout + result.stderr
    assert message in result.stdout


@pytest.mark.parametrize(
    ("name", "fields", "code", "message"),
    [
        ("npm-unclassified", 'reason = "0.3.9 only changes docs"\n', 1, 'needs security = "yes" or "no"'),
        (
            "npm-sec-undecided",
            'reason = "0.3.9 escapes remarks against XSS"\nsecurity = "yes"\n',
            1,
            'a security waiver needs the owner\'s decision, written as owner_decision = "YYYY-MM-DD-<name>"',
        ),
        (
            "npm-sec-decided",
            'reason = "0.3.9 escapes remarks against XSS"\nsecurity = "yes"\nowner_decision = "2026-10-05-kit-hold"\n',
            0,
            "waived by upstream-npm-waivers.toml for 0.3.9",
        ),
        (
            "npm-cve-no",
            'reason = "0.3.9 fixes GHSA-abcd-efgh-ijkl"\nsecurity = "no"\n',
            1,
            "is classified security no, but it names 'GHSA-abcd-efgh-ijkl', which marks a security fix",
        ),
        (
            "npm-classified",
            'reason = "0.3.9 only changes docs"\nsecurity = "no"\n',
            0,
            "waived by upstream-npm-waivers",
        ),
    ],
)
def test_an_npm_waiver_needs_a_security_classification(repo, name, fields, code, message):
    path = repo["path"]
    git(path, "checkout", "-q", "-b", name, "main")
    files = {"upstream-npm-waivers.toml": NPM_WAIVER + fields}
    if name.endswith("-decided"):
        files["UPSTREAM_SYNC.md"] = NPM_RECORD
    commit(path, f"waive npm: {name}", files)
    git(path, "checkout", "-q", "main")
    result = run(
        path,
        "--head",
        name,
        "--check-npm",
        "--npm-latest",
        "@pasarguard/xray-config-kit=0.3.9",
        "--today",
        "2026-10-06",
    )
    assert result.returncode == code, result.stdout + result.stderr
    assert message in result.stdout + result.stderr


DECISION_ID = "2026-10-27-owner-delegated"


def decision_list(*rows, shas=None):
    lines = []
    for row in rows:
        row = row.format(**shas) if shas else row
        ident, _, rest = row.partition(" ")
        lines.append(f"- `{ident}` {rest}".rstrip() + "\n")
    return "\n## Owner decisions\n\n" + "".join(lines)


DELEGATED = (
    f"{DECISION_ID} main chose to let the security ports {{auth}} and {{plain}} wait for the sync until: 2026-11-26"
)


def delegated_repo(tmp_path, name):
    path, shas = security_repo(tmp_path)
    git(path, "update-ref", "refs/remotes/upstream/dev", shas["feature"])
    return path, shas


def delegated_queue(shas, keys=("auth", "plain"), decision=DECISION_ID, until="2026-11-26"):
    lines = "".join(
        f"- `{shas[key]}` port it security: yes owner-decision: {decision} until: {until}\n" for key in keys
    )
    return "## Port queue\n\n" + lines + f"- `{shas['feature']}` port it security: no until: 2026-11-26\n"


@pytest.mark.parametrize("today", ["2026-10-27", "2026-11-26"])
def test_a_security_port_with_a_recorded_owner_decision_is_owed_until_its_date(tmp_path, today):
    path, shas = delegated_repo(tmp_path, f"delegated-{today}")
    result = standalone_gate(path, delegated_queue(shas) + decision_list(DELEGATED, shas=shas), "--today", today)
    assert result.returncode == 0, result.stdout + result.stderr
    assert tally(result.stdout, OWED_LABEL) == 4
    assert "a security port gets no grace" not in result.stdout


def test_a_delegated_security_port_fails_the_day_after_its_date_on_exactly_what_is_unported(tmp_path):
    path, shas = delegated_repo(tmp_path, "delegated-expiry")
    git(path, "cherry-pick", "-x", shas["auth"])
    ledger_text = delegated_queue(shas) + decision_list(DELEGATED, shas=shas)
    last_day = standalone_gate(path, ledger_text, "--today", "2026-11-26")
    assert last_day.returncode == 0, last_day.stdout + last_day.stderr
    assert f"`{shas['auth'][:12]}` is ported now; delete it from the port queue" in last_day.stdout
    pruned = ledger_text.replace(
        f"- `{shas['auth']}` port it security: yes owner-decision: {DECISION_ID} until: 2026-11-26\n", ""
    )
    expired = standalone_gate(path, pruned, "--today", "2026-11-27")
    assert expired.returncode == 1, expired.stdout + expired.stderr
    assert f"`{shas['plain']}` was decided only until 2026-11-26" in expired.stdout
    unported = unported_block(expired.stdout)
    assert shas["plain"][:12] in unported and shas["auth"][:12] not in unported


def test_a_security_port_without_an_owner_decision_still_gets_no_grace(tmp_path):
    path, shas = delegated_repo(tmp_path, "delegated-control")
    queue = (
        "## Port queue\n\n"
        f"- `{shas['plain']}` port it security: yes until: 2026-11-26\n"
        f"- `{shas['auth']}` port it security: yes owner-decision: {DECISION_ID} until: 2026-11-26\n"
        f"- `{shas['feature']}` port it security: no until: 2026-11-26\n"
    )
    result = standalone_gate(path, queue + decision_list(DELEGATED, shas=shas), "--today", "2026-10-27")
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"`{shas['plain'][:12]}` is classified security: yes; a security port gets no grace" in result.stdout
    assert f"`{shas['auth'][:12]}` is classified security: yes; a security port gets no grace" not in result.stdout
    assert shas["plain"][:12] in unported_block(result.stdout)


@pytest.mark.parametrize(
    ("queue_args", "decisions", "message"),
    [
        (
            {"decision": "2026-10-28-early-bird", "until": "2026-11-20"},
            ("2026-10-28-early-bird main decided ahead of time covers {plain} until: 2026-11-20",),
            "names an owner decision dated 2026-10-28, after today",
        ),
        (
            {"decision": "2026-10-01-long-window", "until": "2026-11-05"},
            ("2026-10-01-long-window main gave a long window covers {plain} until: 2026-11-05",),
            "grants grace until 2026-11-05, more than 30 days after its date 2026-10-01",
        ),
        (
            {"decision": "2026-10-27-unrecorded"},
            (DELEGATED,),
            "is not recorded under Owner decisions",
        ),
        (
            {"until": "2026-11-26"},
            (f"{DECISION_ID} main chose a shorter window covers {{plain}} until: 2026-11-10",),
            "is owed until 2026-11-26, after its owner decision 2026-10-27-owner-delegated ends on 2026-11-10",
        ),
        (
            {"decision": "soon"},
            (DELEGATED,),
            "a delegated security port needs the owner's decision, written as owner-decision: YYYY-MM-DD-<name>",
        ),
    ],
)
def test_a_delegated_security_port_is_refused_when_its_decision_is_not_sound(tmp_path, queue_args, decisions, message):
    path, shas = delegated_repo(tmp_path, "delegated-refused")
    result = standalone_gate(
        path,
        delegated_queue(shas, keys=("plain",), **queue_args) + decision_list(*decisions, shas=shas),
        "--today",
        "2026-10-27",
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert message in result.stdout
    assert shas["plain"][:12] in unported_block(result.stdout)


def test_a_security_port_naming_two_owner_decisions_is_refused(tmp_path):
    path, shas = delegated_repo(tmp_path, "delegated-two")
    queue = (
        "## Port queue\n\n"
        f"- `{shas['plain']}` port it security: yes owner-decision: {DECISION_ID} "
        f"owner-decision: 2026-10-26-other until: 2026-11-20\n"
    )
    other = "2026-10-26-other another decision covers {plain} until: 2026-11-20"
    result = standalone_gate(path, queue + decision_list(DELEGATED, other, shas=shas), "--today", "2026-10-27")
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"`{shas['plain']}` names 2 owner decisions; keep exactly one" in result.stdout


@pytest.mark.parametrize(
    ("row", "message"),
    [
        ("not-a-decision some reason until: 2026-11-26", "is not an owner decision id"),
        (f"{DECISION_ID}", "has no reason"),
        (f"{DECISION_ID} no window given", "needs exactly one until: YYYY-MM-DD"),
        (
            f"{DECISION_ID} too long until: 2026-11-27",
            "grants grace until 2026-11-27, more than 30 days after its date",
        ),
        ("2026-10-30-future main decides later until: 2026-11-20", "is dated 2026-10-30, after today"),
        ("2026-02-30-impossible bad date until: 2026-03-01", "has an invalid date"),
    ],
)
def test_a_malformed_owner_decision_record_is_a_ledger_error(tmp_path, row, message):
    path, shas = delegated_repo(tmp_path, "decision-record")
    queue = f"## Port queue\n\n- `{shas['feature']}` port it security: no until: 2026-11-26\n"
    result = standalone_gate(path, queue + decision_list(row), "--today", "2026-10-27")
    assert result.returncode == 1, result.stdout + result.stderr
    assert message in result.stdout


def test_an_owner_decision_recorded_twice_is_a_ledger_error(tmp_path):
    path, shas = delegated_repo(tmp_path, "decision-twice")
    queue = f"## Port queue\n\n- `{shas['feature']}` port it security: no until: 2026-11-26\n"
    result = standalone_gate(path, queue + decision_list(DELEGATED, DELEGATED, shas=shas), "--today", "2026-10-27")
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"`{DECISION_ID}` is recorded more than once" in result.stdout


def test_the_real_ledger_names_a_recorded_decision_on_exactly_its_security_ports():
    text = (SCRIPT.parent.parent / "UPSTREAM_SYNC.md").read_text()
    sys.path.insert(0, str(SCRIPT.parent))
    import upstream_inventory

    entries, _ = upstream_inventory.parse_sync_ledger(str(SCRIPT.parent.parent / "UPSTREAM_SYNC.md"))
    carrying = [entry for entry in entries if DECISION_ID in entry["reason"]]
    assert carrying
    assert all(entry["kind"] == "port-queue" and entry["security"] for entry in carrying)
    queued_yes = [entry for entry in entries if entry["kind"] == "port-queue" and entry["security"]]
    assert all(entry["decision"] and entry["token"] in entry["covers"] for entry in queued_yes)
    decided = [entry for entry in entries if entry["decision"]]
    assert all(entry["security"] and entry["kind"] in ("port-queue", "waived") for entry in decided)
    for ident in {entry["decision"] for entry in decided}:
        assert text.count(f"`{ident}`") == 1


def waived_security(shas, decision, until=""):
    dated = f" until: {until}" if until else ""
    return (
        "## Waived\n\n"
        f"- `{shas['plain']}` the fork pins its own copy security: yes owner-decision: {decision}{dated}\n"
        f"- `{shas['auth']}` the fork rejects such tokens itself security: yes owner-decision: {decision}{dated}\n"
        f"\n## Port queue\n\n- `{shas['feature']}` port it security: no, a header option until: 2026-11-26\n"
    )


@pytest.mark.parametrize(
    ("decision", "records", "today", "message"),
    [
        (
            "2026-10-27-made-up",
            (DELEGATED,),
            "2026-10-27",
            "names owner decision 2026-10-27-made-up, which is not recorded",
        ),
        ("2020-01-01-x", (), "2026-10-27", "names owner decision 2020-01-01-x, which is not recorded"),
        (
            "2026-09-01-old",
            ("2026-09-01-old the fork kept it, covers {auth} {plain} until: 2026-09-30",),
            "2026-10-27",
            "names owner decision 2026-09-01-old, which ended on 2026-09-30",
        ),
        (DECISION_ID, (DELEGATED,), "2026-11-27", f"names owner decision {DECISION_ID}, which ended on 2026-11-26"),
    ],
)
def test_a_waived_security_fix_needs_a_recorded_unexpired_owner_decision(tmp_path, decision, records, today, message):
    path, shas = delegated_repo(tmp_path, "waived-recorded")
    text = waived_security(shas, decision) + (decision_list(*records, shas=shas) if records else "")
    result = standalone_gate(path, text, "--today", today)
    assert result.returncode == 1, result.stdout + result.stderr
    assert message in result.stdout
    unported = unported_block(result.stdout)
    assert shas["plain"][:12] in unported and shas["auth"][:12] in unported


def test_a_waived_security_fix_under_a_recorded_decision_lasts_as_long_as_the_decision(tmp_path):
    path, shas = delegated_repo(tmp_path, "waived-decided")
    text = waived_security(shas, DECISION_ID) + decision_list(DELEGATED, shas=shas)
    green = standalone_gate(path, text, "--today", "2026-10-27")
    assert green.returncode == 0, green.stdout + green.stderr
    assert tally(green.stdout, "waived (ledger)") == 2
    assert "the gate fails on 2026-11-27" in green.stdout
    last_day = standalone_gate(path, text, "--today", "2026-11-26")
    assert last_day.returncode == 0, last_day.stdout + last_day.stderr


def test_a_dated_waiver_of_a_security_fix_may_not_outlast_its_decision(tmp_path):
    path, shas = delegated_repo(tmp_path, "waived-outlasts")
    record = f"{DECISION_ID} a short window, covers {{auth}} {{plain}} until: 2026-11-10"
    text = waived_security(shas, DECISION_ID, until="2026-11-20") + decision_list(record, shas=shas)
    result = standalone_gate(path, text, "--today", "2026-10-27")
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"is decided until 2026-11-20, after its owner decision {DECISION_ID} ends on 2026-11-10" in result.stdout


def test_an_owner_decision_covers_only_the_commits_its_record_lists(tmp_path):
    path, shas = delegated_repo(tmp_path, "decision-scope")
    record = f"{DECISION_ID} only the token fix may wait, covers {{auth}} until: 2026-11-26"
    result = standalone_gate(path, delegated_queue(shas) + decision_list(record, shas=shas), "--today", "2026-10-27")
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"`{shas['plain']}` is not covered by owner decision {DECISION_ID}" in result.stdout
    unported = unported_block(result.stdout)
    assert shas["plain"][:12] in unported and shas["auth"][:12] not in unported


def test_an_owner_decision_record_must_list_what_it_covers(tmp_path):
    path, shas = delegated_repo(tmp_path, "decision-empty")
    record = f"{DECISION_ID} everything may wait until: 2026-11-26"
    result = standalone_gate(path, delegated_queue(shas) + decision_list(record), "--today", "2026-10-27")
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"`{DECISION_ID}` lists no upstream commit or package@version it covers" in result.stdout


@pytest.mark.parametrize(
    ("section", "line"),
    [
        ("Held for an upstream release", "- `{plain}` dev only security: yes owner-decision: " + DECISION_ID + "\n"),
        ("Ported by design", "- `{plain}` the fork has it security: yes owner-decision: " + DECISION_ID + "\n"),
        ("Port queue", "- `{plain}` port it security: no owner-decision: " + DECISION_ID + " until: 2026-11-26\n"),
        ("Waived", "- `{plain}` not taken security: no owner-decision: " + DECISION_ID + "\n"),
    ],
)
def test_an_owner_decision_on_an_entry_that_needs_none_is_refused(tmp_path, section, line):
    path, shas = delegated_repo(tmp_path, "decision-misplaced")
    text = f"## {section}\n\n" + line.format(**shas) + decision_list(DELEGATED, shas=shas)
    result = standalone_gate(path, text, "--today", "2026-10-27")
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"`{shas['plain']}` carries owner-decision: {DECISION_ID} but needs none" in result.stdout


def test_the_ok_line_names_the_first_day_any_dated_decision_fails(tmp_path):
    path, shas = delegated_repo(tmp_path, "earliest-date")
    queue = (
        "## Port queue\n\n"
        f"- `{shas['feature']}` port it security: no, a header option until: 2026-11-26\n"
        f"- `{shas['auth']}` port it security: yes owner-decision: {DECISION_ID} until: 2026-11-26\n"
        "\n## Waived\n\n"
        f"- `{shas['plain']}` the bump waits for the sync security: no until: 2026-11-05\n"
    )
    result = standalone_gate(path, queue + decision_list(DELEGATED, shas=shas), "--today", "2026-10-27")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "the gate fails on 2026-11-06 unless they are ported or re-decided" in result.stdout


def test_the_ok_line_without_owed_ports_still_names_the_first_dated_decision(tmp_path):
    path, shas = delegated_repo(tmp_path, "earliest-waived")
    text = (
        "## Waived\n\n"
        f"- `{shas['plain']}` the bump waits for the sync security: no until: 2026-11-05\n"
        f"- `{shas['feature']}` the fork has its own header option security: no\n"
        f"- `{shas['auth']}` the fork rejects such tokens itself security: yes owner-decision: {DECISION_ID}\n"
    )
    result = standalone_gate(path, text + decision_list(DELEGATED, shas=shas), "--today", "2026-10-27")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Upstream parity gate OK:" in result.stdout
    assert (
        "the earliest dated decision ends on 2026-11-05, so the gate fails on 2026-11-06 unless it is re-decided"
        in (result.stdout)
    )


def test_a_queued_port_classified_security_no_without_a_reason_is_counted(tmp_path):
    path, shas = delegated_repo(tmp_path, "no-reason")
    queue = (
        "## Port queue\n\n"
        f"- `{shas['feature']}` port it security: no until: 2026-11-26\n"
        f"- `{shas['plain']}` port it security: no, a version bump without an advisory until: 2026-11-26\n"
        f"- `{shas['auth']}` port it security: no until: 2026-11-26\n"
    )
    result = standalone_gate(path, queue, "--today", "2026-10-27")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 port-queue, Held or Waived entries classified security: no give no reason for the no" in result.stdout


BRIDGE_SEC = 'reason = "0.9.2 closes an auth bypass; we defer it"\nsecurity = "yes"\n'


@pytest.mark.parametrize(
    ("name", "decision", "ledger", "message"),
    [
        (
            "bridge-unrecorded",
            "2020-01-01-made-up",
            None,
            "names owner decision 2020-01-01-made-up, which is not recorded",
        ),
        (
            "bridge-uncovered",
            "2026-10-05-bridge-hold",
            BRIDGE_RECORD.replace("pasarguard-node-bridge@0.9.2", "pasarguard-node-bridge@0.9.0"),
            "is not covered by owner decision 2026-10-05-bridge-hold",
        ),
        (
            "bridge-expired",
            "2026-10-01-bridge-hold",
            BRIDGE_RECORD.replace("2026-10-05-bridge-hold", "2026-10-01-bridge-hold").replace(
                "2026-11-04", "2026-10-05"
            ),
            "names owner decision 2026-10-01-bridge-hold, which ended on 2026-10-05",
        ),
        (
            "bridge-outlasts",
            "2026-10-05-bridge-hold",
            BRIDGE_RECORD.replace("2026-11-04", "2026-10-10"),
            "runs until 2026-10-20, after its owner decision 2026-10-05-bridge-hold ends on 2026-10-10",
        ),
    ],
)
def test_a_bridge_security_waiver_needs_a_recorded_covering_decision(repo, name, decision, ledger, message):
    path = repo["path"]
    git(path, "checkout", "-q", "-b", name, "main")
    files = {"upstream-bridge-waiver.toml": BRIDGE_WAIVER + BRIDGE_SEC + f'owner_decision = "{decision}"\n'}
    if ledger is not None:
        files["UPSTREAM_SYNC.md"] = ledger
    commit(path, f"waive bridge: {name}", files)
    git(path, "checkout", "-q", "main")
    result = run(path, "--head", name, "--check-bridge", "--bridge-latest", "0.9.1", "--today", "2026-10-06")
    assert result.returncode == 1, result.stdout + result.stderr
    assert message in result.stdout


def test_an_npm_security_waiver_needs_a_recorded_decision(repo):
    path = repo["path"]
    git(path, "checkout", "-q", "-b", "npm-unrecorded", "main")
    fields = 'reason = "0.3.9 escapes remarks against XSS"\nsecurity = "yes"\nowner_decision = "2020-01-01-made-up"\n'
    commit(path, "waive npm: unrecorded", {"upstream-npm-waivers.toml": NPM_WAIVER + fields})
    git(path, "checkout", "-q", "main")
    result = run(
        path,
        "--head",
        "npm-unrecorded",
        "--check-npm",
        "--npm-latest",
        "@pasarguard/xray-config-kit=0.3.9",
        "--today",
        "2026-10-06",
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "names owner decision 2020-01-01-made-up, which is not recorded" in result.stdout + result.stderr


ROUTER = """from fastapi import Depends


@router.get("/admins")
async def list_admins(
    db=Depends(get_db),
    current_admin: AdminDetails = Depends(require_permission("admins", "read")),
):
    return await crud.list(db)


@router.put("/admin/{name}")
async def modify_admin(
    name: str,
    db=Depends(get_db),
    current_admin: AdminDetails = Depends(require_permission("admins", "update")),
):
    return await crud.modify(db, name)


@router.delete("/admin/{name}")
async def remove_admin(
    name: str,
    db=Depends(get_db),
    current_admin: AdminDetails = Depends(require_permission("admins", "read")),
):
    return await crud.remove(db, name)
"""
ROUTER_FIXED = ROUTER.replace(
    'Depends(require_permission("admins", "read")),\n):\n    return await crud.remove',
    'Depends(require_permission("admins", "update")),\n):\n    return await crud.remove',
)
GRANT = "def grant(user, token):\n    if not verify(token):\n        log(user)\n    return issue(user)\n"
GRANT_INDENT_FIXED = (
    "def grant(user, token):\n    if not verify(token):\n        log(user)\n        return issue(user)\n"
)
GRANT_BANNED = GRANT.replace(
    "    return issue(user)", "    if user.banned:\n        raise Forbidden()\n    audit(user)\n    return issue(user)"
)
HUNK_LEDGER = "## Waived\n"


def hunk_repo(tmp_path, name, path, before, after, subject="fix(admin): require update permission to delete admins"):
    repo_path = tmp_path / name
    repo_path.mkdir()
    git(repo_path, "init", "-q", "-b", "up")
    for key, value in (("user.name", "Test"), ("user.email", "test@example.com"), ("commit.gpgsign", "false")):
        git(repo_path, "config", key, value)
    base = commit(repo_path, "upstream base", {path: before})
    git(repo_path, "tag", "v1.0")
    fix = commit(repo_path, subject, {path: after})
    git(repo_path, "tag", "v1.1")
    git(repo_path, "update-ref", "refs/remotes/upstream/dev", fix)
    return repo_path, base, fix


def hunk_gate(repo_path, *extra):
    return standalone_gate(repo_path, HUNK_LEDGER, "--grandfathered-base", "v1.0", *extra)


@pytest.mark.parametrize(
    ("name", "path", "before", "after"),
    [
        ("swap-ours", "router.py", ROUTER, ROUTER_FIXED),
        ("indent-ours", "grant.py", GRANT, GRANT_INDENT_FIXED),
    ],
)
def test_an_ours_merge_of_a_fix_whose_lines_exist_elsewhere_is_not_ported(tmp_path, name, path, before, after):
    repo_path, _, fix = hunk_repo(tmp_path, name, path, before, after)
    git(repo_path, "checkout", "-q", "-b", "main", "v1.0")
    commit(repo_path, "fork feature", {"fork.py": "fork\n"})
    git(repo_path, "merge", "-q", "-s", "ours", "--no-edit", "v1.1")
    assert (repo_path / path).read_text() == before
    result = hunk_gate(repo_path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert fix[:12] in unported_block(result.stdout)
    assert f"{fix[:12]}: merged into main, but 1 of 1 hunk(s)" in result.stdout


def test_a_trailer_whose_pick_dropped_the_hunk_is_not_ported(tmp_path):
    repo_path, _, fix = hunk_repo(tmp_path, "swap-trailer", "router.py", ROUTER, ROUTER_FIXED)
    git(repo_path, "checkout", "-q", "-b", "main", "v1.0")
    commit(repo_path, f"port the admin fix\n\nUpstream: {fix}\n", {"other.py": "x\n"})
    result = hunk_gate(repo_path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert fix[:12] in unported_block(result.stdout)
    assert f"{fix[:12]}: 1 of 1 upstream hunk(s) it changes are not reflected in main" in result.stdout


@pytest.mark.parametrize(
    ("undo", "code"),
    [
        (None, 0),
        ("back-to-read", 1),
        ("raise-moved", 1),
    ],
)
def test_a_copy_without_shared_history_counts_only_while_its_hunks_are_in_place(tmp_path, undo, code):
    path = "router.py" if undo == "back-to-read" else "grant.py"
    before, after = (ROUTER, ROUTER_FIXED) if path == "router.py" else (GRANT, GRANT_BANNED)
    repo_path, _, fix = hunk_repo(tmp_path, f"copy-{undo}", path, before, after)
    git(repo_path, "checkout", "-q", "--orphan", "main", "v1.0")
    git(repo_path, "commit", "-q", "-m", "fork root")
    git(repo_path, "cherry-pick", fix)
    if undo == "back-to-read":
        commit(repo_path, "fork: relax delete again", {path: before})
    elif undo == "raise-moved":
        moved = (
            GRANT_BANNED.replace("        raise Forbidden()\n", "        pass\n")
            + "def other():\n    raise Forbidden()\n"
        )
        commit(repo_path, "fork moves the raise", {path: moved})
    result = hunk_gate(repo_path, "--unrelated-history")
    assert result.returncode == code, result.stdout + result.stderr
    if code:
        assert f"{fix[:12]}: copied into main with the same patch-id, but 1 of 1 hunk(s)" in result.stdout
    else:
        assert tally(result.stdout, "copied into the head, every line present") == 1


def test_a_real_merge_of_the_fix_is_still_ported(tmp_path):
    repo_path, _, _ = hunk_repo(tmp_path, "swap-merge", "router.py", ROUTER, ROUTER_FIXED)
    git(repo_path, "checkout", "-q", "-b", "main", "v1.0")
    commit(repo_path, "fork feature", {"fork.py": "fork\n"})
    git(repo_path, "merge", "-q", "--no-ff", "--no-edit", "v1.1")
    result = hunk_gate(repo_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert tally(result.stdout, "merged into the head, every line present") == 1


IMPORTS = "import os\nimport sys\n\nfrom app.db.base import Base\nfrom app.db.types import Big, Binary, CredentialId\nfrom config import settings\n\nconfig = load()\n"
IMPORTS_FIXED = IMPORTS.replace("Big, Binary, CredentialId", "Big, Binary, Challenge, CredentialId")


@pytest.mark.parametrize(
    ("fork_text", "code"),
    [
        (
            IMPORTS_FIXED.replace(
                "from app.db.base import Base\n", "from app.db.base import Base\nimport app.db.models\n"
            ),
            0,
        ),
        (IMPORTS_FIXED.replace("Challenge, CredentialId", "CredentialId") + "Challenge = None\n", 1),
    ],
)
def test_a_fork_line_inside_a_hunks_context_keeps_the_pick_ported(tmp_path, fork_text, code):
    repo_path, _, fix = hunk_repo(tmp_path, f"context-{code}", "env.py", IMPORTS, IMPORTS_FIXED, "feat(db): add a type")
    git(repo_path, "checkout", "-q", "-b", "main", "v1.0")
    commit(repo_path, f"port the type\n\nUpstream: {fix}\n", {"env.py": fork_text})
    result = hunk_gate(repo_path)
    assert result.returncode == code, result.stdout + result.stderr
    if code == 0:
        assert tally(result.stdout, "ported via cherry-pick -x trailer") == 1
    else:
        assert f"{fix[:12]}: 1 of 1 upstream hunk(s) it changes are not reflected in main" in result.stdout


GUARD = "def allowed(admin, action):\n    return admin.can(action)\n"
GUARD_FIXED = "def allowed(admin, action):\n    if action == 'delete':\n        return admin.can('update')\n    return admin.can(action)\n"


def design_repo(tmp_path, name, undo=False):
    repo_path, _, fix = hunk_repo(tmp_path, name, "router.py", ROUTER, ROUTER_FIXED)
    git(repo_path, "checkout", "-q", "-b", "main", "v1.0")
    commit(repo_path, "fork guard", {"guard.py": GUARD})
    fork_fix = commit(
        repo_path, f"fork: delete needs update in the guard\n\nUpstream: {fix}\n", {"guard.py": GUARD_FIXED}
    )
    if undo:
        commit(repo_path, "fork: drop the guard rule", {"guard.py": GUARD})
    return repo_path, fix, fork_fix


@pytest.mark.parametrize(
    ("cite", "undo", "code"),
    [(True, False, 0), (False, False, 1), (True, True, 1)],
)
def test_a_security_fix_ported_by_design_needs_a_fork_commit_still_in_place(tmp_path, cite, undo, code):
    repo_path, fix, fork_fix = design_repo(tmp_path, f"design-{cite}-{undo}", undo)
    named = f" fork commit {fork_fix}" if cite else ""
    text = f"## Ported by design\n\n- `{fix}` the fork guards delete itself{named} security: yes\n"
    result = standalone_gate(repo_path, text, "--grandfathered-base", "v1.0")
    assert result.returncode == code, result.stdout + result.stderr
    if code:
        assert f"`{fix[:12]}` is ported by design as a security fix, but its reason names no fork commit on main" in (
            result.stdout
        )


@pytest.mark.parametrize(
    ("named_path", "undo_guard", "code"),
    [(" in guard.py:2", False, 0), ("", False, 1), (" in guard.py:2", True, 1), (" in NOTES.md", False, 1)],
)
def test_a_fork_commit_cited_with_its_files_is_checked_in_those_files(tmp_path, named_path, undo_guard, code):
    repo_path, _, fix = hunk_repo(
        tmp_path, f"design-paths-{bool(named_path)}-{undo_guard}", "router.py", ROUTER, ROUTER_FIXED
    )
    git(repo_path, "checkout", "-q", "-b", "main", "v1.0")
    commit(repo_path, "fork guard", {"guard.py": GUARD, "NOTES.md": "guard notes\n\nthe guard checks update\n"})
    fork_fix = commit(
        repo_path,
        f"fork: delete needs update in the guard\n\nUpstream: {fix}\n",
        {"guard.py": GUARD_FIXED, "NOTES.md": "guard notes\n\nthe guard checks update and delete\n"},
    )
    commit(repo_path, "fork: rewrite the notes", {"NOTES.md": "guard notes\n\nsee guard.py\n"})
    if undo_guard:
        commit(repo_path, "fork: drop the guard rule", {"guard.py": GUARD})
    text = f"## Ported by design\n\n- `{fix}` the fork guards delete itself, fork commit {fork_fix}{named_path} security: yes\n"
    result = standalone_gate(repo_path, text, "--grandfathered-base", "v1.0")
    assert result.returncode == code, result.stdout + result.stderr
    if code:
        assert f"`{fix[:12]}` is ported by design as a security fix, but its reason names no fork commit on main" in (
            result.stdout
        )


ROUTER_GUARDED = ROUTER.replace(
    "    return await crud.remove(db, name)", "    guard_delete(current_admin)\n    return await crud.remove(db, name)"
)
ROUTER_PADDED = ROUTER.replace(
    '"read")),\n):\n    return await crud.remove',
    '"read")),\n    audit=Depends(audit_log),\n    trace=Depends(trace_request),\n    limit=Depends(rate_limit),\n):\n'
    "    return await crud.remove",
)
REMOVE_HEAD = '    name: str,\n    db=Depends(get_db),\n    current_admin: AdminDetails = Depends(require_permission("admins", "read")),\n'
ROUTER_PADDED_BOTH = ROUTER.replace(
    REMOVE_HEAD,
    "    name: str,\n    db=Depends(get_db),\n    audit=Depends(audit_log),\n    trace=Depends(trace_request),\n"
    '    limit=Depends(rate_limit),\n    current_admin: AdminDetails = Depends(require_permission("admins", "read")),\n'
    "    tenant=Depends(tenant_scope),\n    locale=Depends(locale_of),\n    clock=Depends(clock_of),\n",
)
ROUTER_NEIGHBOURS = ROUTER.replace(
    REMOVE_HEAD,
    '    name: str,\n    db=Depends(get_session),\n    current_admin: AdminDetails = Depends(require_permission("admins", "read")),\n',
).replace("return await crud.remove(db, name)", "return await crud.remove(db, name, actor=current_admin)")
DESIGN_RELATIONS = {
    "unrelated": ("fork: delete needs update in the guard", {"guard.py": GUARD_FIXED}, "", None, 1),
    "unrelated-named": ("fork: delete needs update in the guard", {"guard.py": GUARD_FIXED}, " in guard.py:2", None, 1),
    "same-file-other-line": (
        "fork: guard delete in the router",
        {"router.py": ROUTER_GUARDED, "guard.py": GUARD_FIXED},
        "",
        None,
        1,
    ),
    "names-the-fix-in-text": (
        "fork: delete needs update in the guard\n\nThe fork's design for {fix}.\n",
        {"guard.py": GUARD_FIXED},
        "",
        None,
        1,
    ),
    "padded-on-both-sides": ("fork: audit admin removal", {"router.py": ROUTER_PADDED_BOTH}, "", None, 1),
    "both-neighbours-edited": ("fork: audit admin removal", {"router.py": ROUTER_NEIGHBOURS}, "", None, 1),
    "padded-around-the-line": ("fork: audit admin removal", {"router.py": ROUTER_PADDED}, "", None, 1),
    "upstream-trailer": (
        "fork: delete needs update in the guard\n\nUpstream: {fix}\n",
        {"guard.py": GUARD_FIXED},
        "",
        None,
        0,
    ),
    "carries-the-change": (
        "fork: delete needs update",
        {"router.py": ROUTER_FIXED, "guard.py": GUARD_FIXED},
        "",
        None,
        0,
    ),
    "carries-the-change-undone": (
        "fork: delete needs update",
        {"router.py": ROUTER_FIXED, "guard.py": GUARD_FIXED},
        "",
        {"router.py": ROUTER},
        1,
    ),
    "carries-the-change-named-file-undone": (
        "fork: delete needs update",
        {"router.py": ROUTER_FIXED, "guard.py": GUARD_FIXED},
        " in guard.py:2",
        {"guard.py": GUARD},
        1,
    ),
}


@pytest.mark.parametrize("relation", sorted(DESIGN_RELATIONS))
def test_a_security_design_port_cites_a_fork_commit_that_carries_the_upstream_change(tmp_path, relation):
    message, files, named, undo, code = DESIGN_RELATIONS[relation]
    repo_path, _, fix = hunk_repo(tmp_path, f"design-relation-{relation}", "router.py", ROUTER, ROUTER_FIXED)
    git(repo_path, "checkout", "-q", "-b", "main", "v1.0")
    commit(repo_path, "fork guard", {"guard.py": GUARD})
    cited = commit(repo_path, message.format(fix=fix), files)
    if undo:
        commit(repo_path, "fork: undo part of it", undo)
    commit(repo_path, "fork: unrelated tidy", {"other.py": "tidy\n"})
    text = f"## Ported by design\n\n- `{fix}` the fork guards delete itself, fork commit {cited}{named} security: yes\n"
    result = standalone_gate(repo_path, text, "--grandfathered-base", "v1.0")
    assert result.returncode == code, result.stdout + result.stderr
    if code:
        assert f"`{fix[:12]}` is ported by design as a security fix, but its reason names no fork commit on main" in (
            result.stdout
        )


TWO_CHECKS = 'def remove(admin, name):\n    check(admin, "read")\n    log(name)\n    audit(admin, "read")\n    return crud.remove(name)\n'
TWO_CHECKS_FIXED = TWO_CHECKS.replace('"read"', '"update"')
TWO_CHECKS_MIDDLE = TWO_CHECKS.replace("    log(name)\n", "    log(name, admin)\n")
LATE_CHECK = "def remove(admin, name):\n    crud.remove(name)\n    notify(name)\n    check(admin)\n    return None\n"
EARLY_CHECK = "def remove(admin, name):\n    check(admin)\n    crud.remove(name)\n    notify(name)\n    return None\n"


@pytest.mark.parametrize(
    ("name", "before", "after", "fork_text", "code"),
    [
        ("middle-line-only", TWO_CHECKS, TWO_CHECKS_FIXED, TWO_CHECKS_MIDDLE, 1),
        ("both-lines", TWO_CHECKS, TWO_CHECKS_FIXED, TWO_CHECKS_FIXED, 0),
        ("moved-check", LATE_CHECK, EARLY_CHECK, EARLY_CHECK, 0),
    ],
)
def test_a_security_design_port_is_judged_by_the_lines_upstream_changed(tmp_path, name, before, after, fork_text, code):
    repo_path, _, fix = hunk_repo(tmp_path, f"design-lines-{name}", "perm.py", before, after)
    git(repo_path, "checkout", "-q", "-b", "main", "v1.0")
    commit(repo_path, "fork guard", {"guard.py": GUARD})
    cited = commit(repo_path, "fork: tighten remove", {"perm.py": fork_text, "guard.py": GUARD_FIXED})
    text = f"## Ported by design\n\n- `{fix}` the fork guards remove itself, fork commit {cited} security: yes\n"
    result = standalone_gate(repo_path, text, "--grandfathered-base", "v1.0")
    assert result.returncode == code, result.stdout + result.stderr


PURGE = "def remove(admin, name):\n    return crud.remove(name)\n\n\ndef purge(admin, name):\n    return crud.remove(name)\n"
PURGE_FIXED = PURGE.replace(
    "def purge(admin, name):\n    return crud.remove(name)",
    "def purge(admin, name):\n    guard(admin)\n    return crud.remove(name)",
)
WIPE = "\n\ndef wipe(admin, name):\n    guard(admin)\n    return crud.remove(name)\n"


def test_a_security_design_port_of_a_line_the_fork_already_has_elsewhere_counts(tmp_path):
    repo_path, _, fix = hunk_repo(tmp_path, "design-added-elsewhere", "purge.py", PURGE, PURGE_FIXED)
    git(repo_path, "checkout", "-q", "-b", "main", "v1.0")
    commit(repo_path, "fork wipe", {"purge.py": PURGE + WIPE})
    cited = commit(repo_path, "fork: guard purge", {"purge.py": PURGE_FIXED + WIPE})
    text = f"## Ported by design\n\n- `{fix}` the fork guards purge itself, fork commit {cited} security: yes\n"
    result = standalone_gate(repo_path, text, "--grandfathered-base", "v1.0")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("files", "code"), [({"notify.yml": "escaped\n"}, 0), ({"notify.yml": "escaped\n", "app.py": "b\n"}, 1)]
)
def test_a_decision_scoped_to_files_stops_covering_a_commit_that_touches_another_file(tmp_path, files, code):
    repo_path = tmp_path / f"scoped-{len(files)}"
    repo_path.mkdir()
    git(repo_path, "init", "-q", "-b", "up")
    for key, value in (("user.name", "Test"), ("user.email", "test@example.com"), ("commit.gpgsign", "false")):
        git(repo_path, "config", key, value)
    commit(repo_path, "upstream base", {"notify.yml": "raw\n", "app.py": "a\n"})
    git(repo_path, "tag", "v1.0")
    notifier = commit(repo_path, "ci(notifications): escape commit messages", files)
    git(repo_path, "update-ref", "refs/remotes/upstream/dev", notifier)
    git(repo_path, "checkout", "-q", "-b", "main", "v1.0")
    git(repo_path, "rm", "-q", "notify.yml")
    git(repo_path, "commit", "-q", "-m", "fork: drop the notifier")
    text = (
        f"## Waived\n\n- `{notifier}` the fork deleted notify.yml security: yes owner-decision: 2026-10-28-hint-waivers\n"
        "\n## Owner decisions\n\n"
        f"- `2026-10-28-hint-waivers` the fork deleted the notifier, covers `{notifier}` touching only `notify.yml` "
        "until: 2026-11-27\n"
    )
    result = standalone_gate(repo_path, text, "--grandfathered-base", "v1.0", "--today", "2026-10-29")
    assert result.returncode == code, result.stdout + result.stderr
    if code:
        assert (
            f"`{notifier}` is not covered by owner decision 2026-10-28-hint-waivers: it touches app.py, outside the "
            "files the record names"
        ) in result.stdout


def test_a_waiver_of_a_security_looking_commit_must_be_security_yes(tmp_path):
    repo_path, _, fix = hunk_repo(tmp_path, "waived-hint", "router.py", ROUTER, ROUTER_FIXED)
    git(repo_path, "checkout", "-q", "-b", "main", "v1.0")
    commit(repo_path, "fork feature", {"fork.py": "fork\n"})
    result = standalone_gate(
        repo_path, f"## Waived\n\n- `{fix}` the fork does not take it security: no\n", "--grandfathered-base", "v1.0"
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"`{fix[:12]}` is waived as security: no, but it names 'permission'" in result.stdout
    assert "a waiver of a security-looking commit needs security: yes and an owner decision" in result.stdout


@pytest.mark.parametrize(
    ("message", "word"),
    [
        (
            (
                "fix(hosts): reject host fields the subscription cannot format\n\n"
                "Host path, SNI and address go through str.format_map with the subscription variables."
            ),
            "format_map",
        ),
        (
            (
                "fix(subscription): keep the server-only finalmask filter out of client configs\n\n"
                "The admin's source IP lists reached users."
            ),
            "server-only",
        ),
        ("fix(api): stop the stats route from leaking other admins' totals", "leaking"),
    ],
)
def test_the_security_hint_knows_format_strings_and_leaks(tmp_path, message, word):
    repo_path = tmp_path / "hint-words"
    repo_path.mkdir()
    git(repo_path, "init", "-q", "-b", "up")
    for key, value in (("user.name", "Test"), ("user.email", "test@example.com"), ("commit.gpgsign", "false")):
        git(repo_path, "config", key, value)
    commit(repo_path, "upstream base", {"hosts.py": "a\n"})
    git(repo_path, "tag", "v1.0")
    fix = commit(repo_path, message, {"hosts.py": "b\n"})
    git(repo_path, "update-ref", "refs/remotes/upstream/dev", fix)
    git(repo_path, "checkout", "-q", "-b", "main", "v1.0")
    commit(repo_path, "fork feature", {"fork.py": "fork\n"})
    queue = f"## Port queue\n\n- `{fix}` port it security: no, a host rebuild change until: 2026-11-20\n"
    result = standalone_gate(repo_path, queue, "--grandfathered-base", "v1.0", "--today", "2026-10-28")
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"`{fix[:12]}` is classified security: no but it names '{word}'; re-review the classification" in (
        result.stdout
    )


def test_the_security_hint_reads_the_commit_body(tmp_path):
    repo_path = tmp_path / "body-hint"
    repo_path.mkdir()
    git(repo_path, "init", "-q", "-b", "up")
    for key, value in (("user.name", "Test"), ("user.email", "test@example.com"), ("commit.gpgsign", "false")):
        git(repo_path, "config", key, value)
    commit(repo_path, "upstream base", {"hosts.py": "a\n"})
    git(repo_path, "tag", "v1.0")
    fix = commit(
        repo_path,
        "fix(hosts): isolate broken hosts\n\nA remark like {USERNAME:>1000000000} was an injection into format_map.",
        {"hosts.py": "b\n"},
    )
    git(repo_path, "update-ref", "refs/remotes/upstream/dev", fix)
    git(repo_path, "checkout", "-q", "-b", "main", "v1.0")
    commit(repo_path, "fork feature", {"fork.py": "fork\n"})
    queue = f"## Port queue\n\n- `{fix}` port it security: no, a host rebuild fix until: 2026-11-20\n"
    result = standalone_gate(repo_path, queue, "--grandfathered-base", "v1.0", "--today", "2026-10-28")
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"`{fix[:12]}` is classified security: no but it names 'injection'; re-review the classification" in (
        result.stdout
    )


def test_a_security_port_error_points_at_a_waiver_under_a_decision(tmp_path):
    path, shas = delegated_repo(tmp_path, "no-grace-wording")
    queue = f"## Port queue\n\n- `{shas['plain']}` port it security: yes until: 2026-11-20\n"
    result = standalone_gate(path, queue, "--today", "2026-10-28")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "a security port gets no grace: port it, or record a Waived entry under an owner decision" in result.stdout


def test_a_short_token_is_checked_against_the_record_by_its_full_sha(tmp_path):
    path, shas = delegated_repo(tmp_path, "short-token")
    plain = shas["plain"]
    other = plain[:12] + ("0" * 28 if plain[12] != "0" else "1" * 28)
    record = decision_list(f"{DECISION_ID} grace for another commit {other} until: 2026-11-20")
    text = f"## Waived\n\n- `{plain[:12]}` deferred security: yes owner-decision: {DECISION_ID}\n" + record
    result = standalone_gate(path, text, "--today", "2026-10-28")
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"`{plain[:12]}` is not covered by owner decision {DECISION_ID}" in result.stdout


@pytest.mark.parametrize(
    ("covers", "code"),
    [
        ("`pasarguard-node-bridge@0.9.2`.", 0),
        ("pasarguard-node-bridge@0.9.2.", 0),
        ("pasarguard-node-bridge@0.9.20", 1),
    ],
)
def test_a_package_cover_matches_exactly_and_ignores_a_closing_dot(repo, covers, code):
    path = repo["path"]
    name = f"bridge-cover-{abs(hash(covers))}"
    git(path, "checkout", "-q", "-b", name, "main")
    ledger_text = (
        f"## Owner decisions\n\n- `2026-10-05-bridge-hold` the bridge waits, covers {covers} until: 2026-11-04\n"
    )
    fields = BRIDGE_SEC + 'owner_decision = "2026-10-05-bridge-hold"\n'
    commit(
        path,
        f"waive bridge: {name}",
        {"upstream-bridge-waiver.toml": BRIDGE_WAIVER + fields, "UPSTREAM_SYNC.md": ledger_text},
    )
    git(path, "checkout", "-q", "main")
    result = run(path, "--head", name, "--check-bridge", "--bridge-latest", "0.9.1", "--today", "2026-10-06")
    assert result.returncode == code, result.stdout + result.stderr


def _release_scope_repo(tmp_path):
    path = tmp_path / "release-scope"
    path.mkdir()
    git(path, "init", "-q", "-b", "up")
    for key, value in (
        ("user.name", "Test"),
        ("user.email", "test@example.com"),
        ("commit.gpgsign", "false"),
        ("tag.gpgsign", "false"),
    ):
        git(path, "config", key, value)
    commit(path, "root", {"r.py": lines("r"), "s.py": lines("s")})
    git(path, "tag", "v1.0")
    git(path, "update-ref", "refs/upstream-tags/v1.0", git(path, "rev-parse", "v1.0"))
    git(path, "update-ref", "refs/remotes/upstream/main", git(path, "rev-parse", "v1.0"))
    git(path, "checkout", "-q", "-b", "rel", "v1.0")
    released = commit(path, "upstream renames a local in the loader", {"r.py": lines("r", overrides={2: "R2"})})
    git(path, "tag", "-a", "v1.1", "-m", "v1.1")
    git(path, "update-ref", "refs/upstream-tags/v1.1", git(path, "rev-parse", "v1.1^{commit}"))
    git(path, "checkout", "-q", "-b", "devline", "v1.0")
    dev = commit(path, "upstream reorders the loader list", {"s.py": lines("s", overrides={3: "S3"})})
    git(path, "update-ref", "refs/remotes/upstream/dev", dev)
    git(path, "checkout", "-q", "-b", "side", "v1.0")
    side = commit(path, "upstream drafts a wider loader list", {"s.py": lines("s", overrides={7: "S7"})})
    git(path, "update-ref", "refs/remotes/upstream/feat/x", side)
    git(path, "checkout", "-q", "--orphan", "main", "v1.0")
    git(path, "commit", "-q", "-m", "fork root")
    git(path, "branch", "-q", "-D", "up", "rel", "devline", "side")
    return path, released, dev, side


def _scope_gate(path, ledger_text, *extra):
    (path / "UPSTREAM_SYNC.md").write_text(ledger_text)
    return run(
        path,
        "--base",
        "v1.0",
        "--head",
        "main",
        "--track",
        "refs/remotes/upstream/*",
        "--track",
        "refs/upstream-tags/*",
        "--fail-on-unported",
        "UPSTREAM_SYNC.md",
        "--release-scope-ref",
        "refs/remotes/upstream/main",
        "--release-scope-ref",
        "refs/upstream-tags/*",
        "--today",
        "2026-10-06",
        *extra,
    )


RELEASED_WAIVER = "fork ships the same rename as its own code security: no, it renames one local variable"
DEVONLY_TALLY = "dev-only (reported, not blocking)"


def test_a_commit_only_a_release_tag_carries_still_fails_the_gate(tmp_path):
    path, released, _, _ = _release_scope_repo(tmp_path)

    result = _scope_gate(path, "## Waived\n")

    assert result.returncode == 1, result.stdout + result.stderr
    assert released[:12] in unported_block(result.stdout)


def test_a_dev_only_upstream_commit_is_reported_without_failing_the_gate(tmp_path):
    path, released, dev, side = _release_scope_repo(tmp_path)

    result = _scope_gate(path, f"## Waived\n\n- `{released}` {RELEASED_WAIVER}\n")

    assert result.returncode == 0, result.stdout + result.stderr
    assert tally(result.stdout, "unported") == 0
    assert tally(result.stdout, DEVONLY_TALLY) == 2
    block = result.stdout.split("Dev-only upstream commits", 1)[1].split("\n\n", 1)[0]
    assert dev[:12] in block
    assert side[:12] in block
    assert "turns unported the day a release carries it" in result.stdout


def test_a_dev_only_commit_blocks_again_once_a_release_tag_carries_it(tmp_path):
    path, released, dev, _ = _release_scope_repo(tmp_path)
    git(path, "update-ref", "refs/upstream-tags/v1.2", dev)

    result = _scope_gate(path, f"## Waived\n\n- `{released}` {RELEASED_WAIVER}\n")

    assert result.returncode == 1, result.stdout + result.stderr
    assert dev[:12] in unported_block(result.stdout)
    assert tally(result.stdout, DEVONLY_TALLY) == 1


def test_a_dev_only_commit_blocks_again_once_upstream_main_carries_it(tmp_path):
    path, released, dev, _ = _release_scope_repo(tmp_path)
    git(path, "update-ref", "refs/remotes/upstream/main", dev)

    result = _scope_gate(path, f"## Waived\n\n- `{released}` {RELEASED_WAIVER}\n")

    assert result.returncode == 1, result.stdout + result.stderr
    assert dev[:12] in unported_block(result.stdout)
    assert tally(result.stdout, DEVONLY_TALLY) == 1


def test_a_prerelease_tag_does_not_pull_a_dev_only_commit_into_release_scope(tmp_path):
    path, released, dev, _ = _release_scope_repo(tmp_path)
    git(path, "update-ref", "refs/upstream-tags/v1.2-rc1", dev)

    result = _scope_gate(path, f"## Waived\n\n- `{released}` {RELEASED_WAIVER}\n")

    assert result.returncode == 0, result.stdout + result.stderr
    assert tally(result.stdout, DEVONLY_TALLY) == 2


def test_a_held_entry_for_a_dev_only_commit_is_reported_as_no_longer_needed(tmp_path):
    path, released, dev, _ = _release_scope_repo(tmp_path)
    ledger_text = (
        f"## Waived\n\n- `{released}` {RELEASED_WAIVER}\n\n"
        "## Held for an upstream release\n\n"
        f"- `{dev}` dev-only, held for the next upstream release security: no, it only reorders a list\n"
    )

    result = _scope_gate(path, ledger_text)

    assert result.returncode == 0, result.stdout + result.stderr
    assert tally(result.stdout, "held until an upstream release (ledger)") == 1
    assert tally(result.stdout, DEVONLY_TALLY) == 1
    assert (
        f"`{dev[:12]}` is carried only by upstream dev or a side branch, so this entry is no longer needed"
        in result.stdout
    )


SCOPE_HEADS = (
    ("refs/remotes/upstream/main", "upstream/main"),
    ("refs/remotes/upstream/dev", "upstream/dev"),
    ("refs/remotes/upstream/feat/x", "upstream/feat/x"),
    ("refs/upstream-tags/v1.0", "v1.0"),
    ("refs/upstream-tags/v1.1", "v1.1"),
)


def _record_heads(path, skip):
    text = "".join(
        f"{git(path, 'rev-parse', ref + '^{commit}')} {name}\n" for ref, name in SCOPE_HEADS if name != skip
    )
    (path / ".upstream-refs").write_text(text)


def _snapshot_scope_gate(path, released):
    return _scope_gate(path, f"## Waived\n\n- `{released}` {RELEASED_WAIVER}\n", "--refs-snapshot", ".upstream-refs")


def test_an_unrecorded_dev_head_is_reported_without_failing_the_gate(tmp_path):
    path, released, dev, _ = _release_scope_repo(tmp_path)
    _record_heads(path, skip="upstream/dev")

    result = _snapshot_scope_gate(path, released)

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"upstream head upstream/dev at {dev[:12]} is not recorded in .upstream-refs" in result.stdout
    assert "no release tag and not upstream main carry it, so it is reported and not blocking" in result.stdout


def test_an_unrecorded_upstream_main_head_still_fails_the_gate(tmp_path):
    path, released, _, _ = _release_scope_repo(tmp_path)
    git(path, "update-ref", "refs/remotes/upstream/main", released)
    _record_heads(path, skip="upstream/main")

    result = _snapshot_scope_gate(path, released)

    assert result.returncode == 1, result.stdout + result.stderr
    assert f"upstream head upstream/main at {released[:12]} is not recorded in .upstream-refs" in result.stdout
    assert "refs snapshot error(s)" in result.stdout


def test_an_unrecorded_release_tag_head_still_fails_the_gate(tmp_path):
    path, released, _, _ = _release_scope_repo(tmp_path)
    _record_heads(path, skip="v1.1")

    result = _snapshot_scope_gate(path, released)

    assert result.returncode == 1, result.stdout + result.stderr
    assert f"upstream head v1.1 at {released[:12]} is not recorded in .upstream-refs" in result.stdout
