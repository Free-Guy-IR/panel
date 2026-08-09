"""Being on several nodes is only reported once it has held for a few checks.

A client that tries every server reaches a high node count the moment it
refreshes and drops back on the next cycle. Someone genuinely reaching more
than one node stays there. Counting how many checks the pattern survives is
what tells the two apart, so a single spike must say nothing at all.
"""

from types import SimpleNamespace

import pytest

from app.models.settings import ConnectionLimit
from app.utils.connection_limiter import NodeActivity, assess

USER = SimpleNamespace(id=7, username="someone")
NOW = 1_800_000_000
# One address, so nothing else in the evidence moves while the nodes do.
LIVE = {"5.115.21.4": NOW}


def _settings(**overrides) -> ConnectionLimit:
    return ConnectionLimit(**{"cdn_ranges": [], **overrides})


def _assess(node_ids, prior_streak, settings=None):
    # Every node in these cases carried enough to count; the threshold has its
    # own tests, and mixing the two would test both badly.
    carried = NodeActivity(
        touched=set(node_ids),
        used=set(node_ids),
        # One at a time here: simultaneity has its own tests, and mixing the
        # two would test both badly.
        concurrent=1,
        concurrent_nodes={next(iter(node_ids), 0): 50 * 1024 * 1024} if node_ids else {},
    )
    return assess(
        USER,
        LIVE,
        set(node_ids),
        carried,
        set(),
        frozenset(),
        [],
        {"apps": set(), "hwids": set()},
        {},
        settings or _settings(),
        None,
        prior_streak,
    )


def _nodes_reason(obs):
    return next((r for r in obs.reasons if r.get("code") == "nodes"), None)


@pytest.mark.parametrize("prior, expected_streak", [(0, 1), (1, 2)])
def test_a_short_run_is_not_reported(prior, expected_streak):
    """Under the threshold, three nodes are as likely to be one refresh."""
    obs = _assess({1, 2, 3}, prior)
    assert obs.node_streak == expected_streak
    assert _nodes_reason(obs) is None


def test_the_run_is_reported_once_it_has_held():
    obs = _assess({1, 2, 3}, 2)
    assert obs.node_streak == 3
    assert _nodes_reason(obs) == {"code": "nodes", "count": 3, "cycles": 3}


def test_dropping_back_to_one_node_ends_the_run():
    """A spike that does not repeat leaves nothing behind for the next cycle."""
    obs = _assess({1}, 5)
    assert obs.node_streak == 0
    assert _nodes_reason(obs) is None


def test_the_threshold_is_the_setting_and_not_a_constant():
    obs = _assess({1, 2}, 0, _settings(persistence_cycles=1))
    assert _nodes_reason(obs) == {"code": "nodes", "count": 2, "cycles": 1}


def test_the_run_never_changes_the_verdict():
    """The nodes line explains a count; it is not evidence of another device."""
    held = _assess({1, 2, 3, 4}, 10)
    fresh = _assess({1}, 0)
    assert held.devices == fresh.devices
    assert held.verdict == fresh.verdict
