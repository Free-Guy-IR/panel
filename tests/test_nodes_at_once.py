"""Working down the server list is one person, not one person per server.

Connect, the speed is poor, move on: that can leave real traffic on three
nodes inside one ten-minute bucket, and the bucket cannot tell it from three
devices. The nodes can - a node stops reporting someone who has left it - so
two nodes have to be holding the user at the same moment, and both have to
have carried real traffic.
"""

from types import SimpleNamespace

import pytest

from app.models.settings import ConnectionLimit
from app.utils.connection_limiter import NodeActivity, assess

NOW = 1_800_000_000
USER = SimpleNamespace(id=1, username="someone")
MB = 1024 * 1024


def _assess(seen_by_node, used, bucket_traffic=None, addresses=("5.115.21.4",), sustained=True, **overrides):
    """sustained=True stands in for the overlap having already held several
    checks - a real second device. sustained=False is the first check of an
    overlap, which is where a node switch shows up."""
    activity = NodeActivity(
        touched=set(used),
        used=set(used),
        concurrent=len(bucket_traffic or {}),
        concurrent_nodes=bucket_traffic or {},
        traffic=dict(bucket_traffic or {}),
    )
    return assess(
        USER,
        {ip: NOW for ip in addresses},
        seen_by_node,
        set(used),
        activity,
        set(),
        frozenset(),
        [],
        {"apps": set(), "hwids": set()},
        {},
        ConnectionLimit(cdn_ranges=[], **overrides),
        device_limit=None,
        prior_node_streak=0,
        # Enough prior overlap that this check crosses the persistence bar.
        prior_at_once_streak=5 if sustained else 0,
    )


def _reason(obs, code):
    return next((r for r in obs.reasons if r.get("code") == code), None)


def test_someone_working_down_the_server_list_is_one_device():
    """Real traffic on three nodes in one bucket, but only the last still has them."""
    obs = _assess(
        seen_by_node={21: NOW - 600, 23: NOW - 300, 30: NOW},
        used={21, 23, 30},
        bucket_traffic={21: 4 * MB, 23: 6 * MB, 30: 20 * MB},
    )
    assert obs.devices == 1
    assert _reason(obs, "nodes_at_once") is None


def test_two_nodes_held_across_several_checks_is_two_devices():
    obs = _assess(
        seen_by_node={21: NOW - 5, 23: NOW},
        used={21, 23},
        bucket_traffic={21: 212 * MB, 23: 165 * MB},
        sustained=True,
    )
    assert obs.devices == 2
    assert _reason(obs, "nodes_at_once")["count"] == 2


def test_a_first_overlap_is_a_node_switch_not_a_second_device():
    """The reported case: one person downloads, the speed is poor, they move to
    another node. The old node lingers, so two are seen at once for a moment -
    but it does not hold, so it is not counted."""
    obs = _assess(
        seen_by_node={16: NOW - 3, 40: NOW},
        used={16, 40},
        bucket_traffic={16: 332 * MB, 40: 3 * MB},
        sustained=False,
    )
    assert obs.devices == 1
    assert _reason(obs, "nodes_at_once") is None
    pending = _reason(obs, "nodes_at_once_pending")
    assert pending is not None and pending["count"] == 2


def test_a_node_that_was_only_tried_does_not_count_even_while_still_reported():
    """Reported now, but it never carried anything worth calling use."""
    obs = _assess(seen_by_node={21: NOW, 23: NOW}, used={21}, bucket_traffic={21: 40 * MB})
    assert obs.devices == 1
    assert _reason(obs, "nodes_at_once") is None


def test_the_window_is_the_concurrency_setting():
    """Just outside it, the earlier node is the same person a moment ago."""
    inside = _assess(seen_by_node={21: NOW - 80, 23: NOW}, used={21, 23},
                     bucket_traffic={21: 10 * MB, 23: 10 * MB})
    outside = _assess(seen_by_node={21: NOW - 100, 23: NOW}, used={21, 23},
                      bucket_traffic={21: 10 * MB, 23: 10 * MB})
    assert inside.devices == 2
    assert outside.devices == 1


def test_three_nodes_at_once_counts_three():
    obs = _assess(
        seen_by_node={26: NOW, 30: NOW - 2, 40: NOW - 1},
        used={26, 30, 40},
        bucket_traffic={26: 29 * MB, 30: 18 * MB, 40: 110 * MB},
    )
    assert obs.devices == 3
    assert obs.verdict == "over_limit"


def test_the_addresses_still_win_when_they_see_more():
    obs = _assess(
        seen_by_node={21: NOW},
        used={21},
        bucket_traffic={21: 40 * MB},
        addresses=["5.115.21.4", "83.121.230.9", "2.179.8.1"],
    )
    assert obs.devices == 3


@pytest.mark.parametrize("nodes", [{}, {21: NOW}])
def test_fewer_than_two_nodes_is_never_a_reason_to_count_anyone(nodes):
    obs = _assess(seen_by_node=nodes, used=set(nodes), bucket_traffic={n: 40 * MB for n in nodes},
                  addresses=())
    assert obs.devices == 0


def test_a_connected_node_below_the_threshold_is_shown_not_hidden():
    """The node a user is on always appears in the evidence, even when it is
    under the counting threshold - shown with a mark, not counted."""
    from app.utils.connection_limiter import NodeActivity, assess
    activity = NodeActivity(touched={21}, used=set(), concurrent=0, concurrent_nodes={}, traffic={21: 96_000})
    obs = assess(
        USER,
        {},
        {},
        set(),
        activity,
        set(),
        frozenset(),
        [],
        {"apps": set(), "hwids": set()},
        {},
        ConnectionLimit(cdn_ranges=[], node_min_traffic_kb=10024),
        node_labels={21: "141.94.92.71"},
    )
    nt = _reason(obs, "node_traffic")
    assert nt is not None
    assert nt["count"] == 1 and nt["counted"] == 0
    assert any("141.94.92.71" in it and "*" in it for it in nt["items"])
    # It is shown but contributes nothing to the device count.
    assert obs.devices == 0
