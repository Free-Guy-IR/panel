import pytest

from tests.test_hostile_10m import load_inventory
from tests.test_hostile_10q import commit, git

T_BASE = {
    "a.py": "import os\n\n\ndef guard(x):\n    return x\n\n\ndef count(q):\n    return q\n",
    "b.py": "import sys\n\n\ndef check(y):\n    return y\n",
}
T_PICK = {
    "a.py": (
        "import os\n\n\ndef guard(x):\n    validate(x)\n    return x\n\n\ndef count(q):\n"
        "    if use_pairs(q):\n        return pairs(q)\n    return q\n"
    ),
    "b.py": "import sys\n\n\ndef check(y):\n    sanitize(y)\n    return y\n",
}
T_LATER = {
    "a.py": (
        "import os\n\n\ndef guard(x):\n    validate(x)\n    return x\n\n\ndef count(q):\n"
        "    return single_pass(q)\n    return q\n"
    ),
}
T_OTHER = {"c.txt": "notes\n"}


def partial_world(tmp_path, later=T_LATER, ported=T_LATER, trailer="later", order="after"):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "fork")
    git(repo, "config", "commit.gpgsign", "false")
    commit(repo, "base", T_BASE)
    git(repo, "checkout", "-q", "-b", "up")
    if order == "before":
        replaced = commit(repo, "perf: single pass", later)
        superseded = commit(repo, "security: validate input", T_PICK)
    else:
        superseded = commit(repo, "security: validate input", T_PICK)
        replaced = commit(repo, "perf: single pass", later)
    git(repo, "update-ref", "refs/remotes/upstream/main", "up")
    git(repo, "checkout", "-q", "fork")
    pick = commit(repo, f"security: validate input\n\n(cherry picked from commit {superseded})", T_PICK)
    named = {"later": f"\n\n(cherry picked from commit {replaced})", "none": ""}[trailer]
    head = commit(repo, f"perf: single pass{named}", ported)
    return repo, superseded, replaced, pick, head


def test_10t_later_ported_upstream_removal_is_accepted(tmp_path, monkeypatch):
    repo, superseded, _, pick, head = partial_world(tmp_path)
    module = load_inventory()
    monkeypatch.chdir(repo)
    reason = f"Ported by its cherry-pick -x {pick[:12]}. security: yes"
    assert module.security_design_problems(reason, head, superseded, {}) == []


@pytest.mark.parametrize(
    "case",
    ["fork-only-removal", "later-not-ported", "later-removes-other-lines", "later-is-older"],
)
def test_10t_partial_supersession_stays_strict(tmp_path, monkeypatch, case):
    fork_only = {"a.py": T_LATER["a.py"]}
    kwargs = {
        "fork-only-removal": {"later": T_OTHER, "ported": fork_only, "trailer": "none"},
        "later-not-ported": {"ported": fork_only, "trailer": "none"},
        "later-removes-other-lines": {
            "later": {"a.py": T_PICK["a.py"].replace("import os\n", "")},
            "ported": {"a.py": T_LATER["a.py"].replace("import os\n", "")},
        },
        "later-is-older": {"later": {"c.txt": "notes\n"}, "ported": {**fork_only, "c.txt": "notes\n"}, "order": "before"},
    }[case]
    repo, superseded, _, pick, head = partial_world(tmp_path, **kwargs)
    module = load_inventory()
    monkeypatch.chdir(repo)
    reason = f"Ported by its cherry-pick -x {pick[:12]}. security: yes"
    problems = module.security_design_problems(reason, head, superseded, {})
    assert problems is None or any("a.py:+" in problem for problem in problems)
