from types import SimpleNamespace

import pytest

from app.models.settings import ConnectionLimit
from app.utils.connection_limiter import NodeActivity, assess

USER = SimpleNamespace(id=7, username="someone")
NOW = 1_800_000_000
LIVE = {"5.115.21.4": NOW}


def _settings(**overrides) -> ConnectionLimit:
    return ConnectionLimit(**{"cdn_ranges": [], **overrides})


def _assess(node_ids, prior_streak, settings=None):
    carried = NodeActivity(
        touched=set(node_ids),
        used=set(node_ids),
        concurrent=1,
        concurrent_nodes={next(iter(node_ids), 0): 50 * 1024 * 1024} if node_ids else {},
    )
    return assess(
        USER,
        LIVE,
        {},
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
    obs = _assess({1, 2, 3}, prior)
    assert obs.node_streak == expected_streak
    assert _nodes_reason(obs) is None


def test_the_run_is_reported_once_it_has_held():
    obs = _assess({1, 2, 3}, 2)
    assert obs.node_streak == 3
    assert _nodes_reason(obs) == {"code": "nodes", "count": 3, "cycles": 3}


def test_dropping_back_to_one_node_ends_the_run():
    obs = _assess({1}, 5)
    assert obs.node_streak == 0
    assert _nodes_reason(obs) is None


def test_the_threshold_is_the_setting_and_not_a_constant():
    obs = _assess({1, 2}, 0, _settings(persistence_cycles=1))
    assert _nodes_reason(obs) == {"code": "nodes", "count": 2, "cycles": 1}


def test_the_run_never_changes_the_verdict():
    held = _assess({1, 2, 3, 4}, 10)
    fresh = _assess({1}, 0)
    assert held.devices == fresh.devices
    assert held.verdict == fresh.verdict
