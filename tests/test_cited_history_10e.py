import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "upstream_inventory.py"
UPSTREAM_GONE = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
SIDE = "f0e1d2c3b4a5968778695a4b3c2d1e0f98765432"
SELF = "0badc0ffee0badc0ffee0badc0ffee0badc0ffee"


@pytest.fixture
def inventory(monkeypatch):
    spec = importlib.util.spec_from_file_location("upstream_inventory_10e", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    known = {SIDE: SIDE}
    monkeypatch.setattr(module, "resolve_commit", lambda token: known.get(token))
    monkeypatch.setattr(module, "is_ancestor", lambda ancestor, descendant: False)
    monkeypatch.setattr(module, "on_upstream_ref", lambda sha: False)
    return module


def entry(token, reason):
    return {"where": "kit.md:3", "token": token, "reason": reason}


def test_an_upstream_sha_from_a_deleted_branch_tracked_in_the_ledger_is_not_a_fork_cite(inventory):
    cite = entry(SIDE[:12], f"carried with fork commit {UPSTREAM_GONE[:12]} security: no")
    assert inventory.cited_history_problems(cite, "main", frozenset({UPSTREAM_GONE, SIDE[:12]})) == []


def test_an_untracked_unknown_sha_cited_as_a_fork_commit_is_still_flagged(inventory):
    cite = entry(SIDE[:12], f"carried with fork commit {UPSTREAM_GONE[:12]} security: no")
    assert inventory.cited_history_problems(cite, "main", frozenset({SIDE[:12]})) == [
        f"kit.md:3: `{SIDE[:12]}` cites fork commit {UPSTREAM_GONE[:12]}, which is not in the history of main"
    ]


def test_an_entry_citing_its_own_upstream_sha_is_not_a_fork_cite(inventory):
    cite = entry(SELF, f"waived in fork commit {SELF[:12]} security: no")
    assert inventory.cited_history_problems(cite, "main", frozenset({SELF})) == []


@pytest.mark.parametrize(
    ("reason", "flagged"),
    [
        (f"ported in fork commit {SIDE[:12]} on branch `fork/hy2-rework`, not on main yet", False),
        (f"ported in fork commit {SIDE[:12]}, not on main yet", True),
    ],
    ids=["branch-named", "branch-unnamed"],
)
def test_a_side_branch_fork_commit_is_excused_only_when_its_branch_is_named(inventory, reason, flagged):
    cite = entry(SELF, f"{reason} security: no")
    problems = inventory.cited_history_problems(cite, "main", frozenset({SELF}))
    assert bool(problems) is flagged, problems
