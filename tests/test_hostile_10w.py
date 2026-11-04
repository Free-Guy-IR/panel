from tests.test_hostile_10m import load_inventory
from tests.test_hostile_10q import commit, git

BASE = (
    "def first(admin):\n    load(admin)\n    return admin\n\n\ndef second(admin):\n    load(admin)\n    return admin\n"
)
UPSTREAM_PICK = BASE.replace(
    "    load(admin)\n    return admin\n\n\ndef second",
    "    load(admin)\n    require_admin(admin)\n    return admin\n\n\ndef second",
)
FORK_BASE = "def first(admin):\n    load(admin)\n    return admin\n\n\ndef second(admin):\n    load(admin)\n    require_admin(admin)\n    return admin\n"
FORK_PICK = FORK_BASE.replace(
    "    load(admin)\n    return admin\n\n\ndef second",
    "    load(admin)\n    require_admin(admin)\n    return admin\n\n\ndef second",
)
FORK_TWEAK = FORK_PICK.replace(
    "    load(admin)\n    require_admin(admin)\n    return admin\n\n\ndef second",
    "    load(admin, fast=True)\n    require_admin(admin)\n    return admin\n\n\ndef second",
)
FORK_PORTED = FORK_TWEAK.replace(
    "def second(admin):\n    load(admin)\n    require_admin(admin)\n", "def second(admin):\n    load(admin)\n"
)
FORK_HEAD = FORK_PORTED.replace(
    "    load(admin, fast=True)\n    require_admin(admin)\n", "    load(admin, fast=True)\n"
)


def wrong_spot_world(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "fork")
    git(repo, "config", "commit.gpgsign", "false")
    commit(repo, "base", {"a.py": BASE})
    git(repo, "checkout", "-q", "-b", "up")
    superseded = commit(repo, "security: require admin", {"a.py": UPSTREAM_PICK})
    later = commit(repo, "refactor: tidy checks", {"a.py": BASE})
    git(repo, "update-ref", "refs/remotes/upstream/main", "up")
    git(repo, "checkout", "-q", "fork")
    commit(repo, "fork: guard second", {"a.py": FORK_BASE})
    pick = commit(repo, f"security: require admin\n\n(cherry picked from commit {superseded})", {"a.py": FORK_PICK})
    commit(repo, "fork: tweak", {"a.py": FORK_TWEAK})
    ported = commit(repo, f"refactor: tidy checks\n\n(cherry picked from commit {later})", {"a.py": FORK_PORTED})
    head = commit(repo, "chore: simplify", {"a.py": FORK_HEAD})
    return repo, superseded, later, pick, ported, head


def test_10w_wrong_unique_spot_after_a_fork_tweak_is_refused_at_row_level(tmp_path, monkeypatch):
    repo, superseded, _, pick, _, head = wrong_spot_world(tmp_path)
    module = load_inventory()
    monkeypatch.chdir(repo)
    problems = module.security_design_problems(
        f"Ported by its cherry-pick -x {pick[:12]}. security: yes", head, superseded, {}
    )
    assert problems
    assert any("a.py:+3" in problem for problem in problems)


def test_10w_later_hunk_at_a_spot_other_than_the_picks_own_is_not_brought(tmp_path, monkeypatch):
    repo, superseded, later, _, ported, head = wrong_spot_world(tmp_path)
    module = load_inventory()
    monkeypatch.chdir(repo)
    hunks = module.commit_hunks(later)
    assert len(hunks) == 1
    assert module.later_hunk_brought(ported, "a.py", hunks[0], superseded) is False
    assert module.ported_later_changes(superseded, head, "a.py") == ()


def test_10w_later_hunk_at_the_picks_own_spot_is_brought(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "fork")
    git(repo, "config", "commit.gpgsign", "false")
    commit(repo, "base", {"a.py": BASE})
    git(repo, "checkout", "-q", "-b", "up")
    superseded = commit(repo, "security: require admin", {"a.py": UPSTREAM_PICK})
    later = commit(repo, "refactor: tidy checks", {"a.py": BASE})
    git(repo, "update-ref", "refs/remotes/upstream/main", "up")
    git(repo, "checkout", "-q", "fork")
    commit(repo, "fork: guard second", {"a.py": FORK_BASE})
    commit(repo, f"security: require admin\n\n(cherry picked from commit {superseded})", {"a.py": FORK_PICK})
    head = commit(repo, f"refactor: tidy checks\n\n(cherry picked from commit {later})", {"a.py": FORK_BASE})
    module = load_inventory()
    monkeypatch.chdir(repo)
    hunks = module.commit_hunks(later)
    assert len(hunks) == 1
    assert module.later_hunk_brought(head, "a.py", hunks[0], superseded) is True
    assert [sha for sha, _ in module.ported_later_changes(superseded, head, "a.py")] == [later]
