"""Two nodes at the same moment is two devices; one after the other is one.

A person uses one node at a time. Summing a window could not tell twenty
minutes on one node followed by ten on another from two people on both
throughout, and the audit found 30 users the address count had missed because
of it - among them #18022, carrying 110MB, 30MB and 18MB on three nodes inside
a single ten-minute bucket while being counted as two devices.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models.settings import ConnectionLimit
from app.utils.connection_limiter import NodeActivity, assess

NOW = 1_800_000_000
USER = SimpleNamespace(id=18022, username="someone")
MB = 1024 * 1024


def _assess(activity, addresses=(), hwids=(), **overrides):
    settings = ConnectionLimit(cdn_ranges=[], **overrides)
    return assess(
        USER,
        {ip: NOW for ip in addresses},
        activity.used,
        activity,
        set(),
        frozenset(),
        [],
        {"apps": set(), "hwids": set(hwids)},
        {},
        settings,
        None,
        0,
    )


def _reason(obs, code):
    return next((r for r in obs.reasons if r.get("code") == code), None)


def test_the_audited_case_of_three_nodes_at_once_counts_three():
    activity = NodeActivity(
        touched={26, 30, 40},
        used={26, 30, 40},
        concurrent=3,
        concurrent_nodes={26: 29 * MB, 30: 18 * MB, 40: 110 * MB},
    )
    obs = _assess(activity, addresses=["5.115.21.4"])
    assert obs.devices == 3
    assert obs.verdict == "over_limit"


def test_moving_between_servers_is_still_one_device():
    """Two nodes used over the window, but never in the same bucket."""
    activity = NodeActivity(touched={21, 30}, used={21, 30}, concurrent=1, concurrent_nodes={30: 40 * MB})
    obs = _assess(activity, addresses=["5.115.21.4"])
    assert obs.devices == 1


def test_the_addresses_still_win_when_they_see_more():
    activity = NodeActivity(touched={21}, used={21}, concurrent=1, concurrent_nodes={21: 40 * MB})
    obs = _assess(activity, addresses=["5.115.21.4", "83.121.230.9", "2.179.8.1"])
    assert obs.devices == 3


def test_two_nodes_at_once_beat_a_single_address():
    """A carrier pool, a CDN and a NAT all blur an address. None blurs this."""
    activity = NodeActivity(touched={21, 23}, used={21, 23}, concurrent=2,
                            concurrent_nodes={21: 212 * MB, 23: 165 * MB})
    obs = _assess(activity, addresses=["5.115.21.4"])
    assert obs.address_sources == 1
    assert obs.devices == 2


def test_it_counts_even_when_no_address_came_back():
    """Traffic on two nodes is evidence whether or not the IP list answered."""
    activity = NodeActivity(touched={21, 23}, used={21, 23}, concurrent=2,
                            concurrent_nodes={21: 212 * MB, 23: 165 * MB})
    obs = _assess(activity)
    assert obs.address_sources == 0
    assert obs.devices == 2


def test_one_node_alone_is_never_a_reason_to_count_anyone():
    activity = NodeActivity(touched={21}, used={21}, concurrent=1, concurrent_nodes={21: 500 * MB})
    obs = _assess(activity)
    assert obs.devices == 0


def test_the_evidence_names_the_nodes_and_what_each_carried():
    activity = NodeActivity(touched={21, 23}, used={21, 23}, concurrent=2,
                            concurrent_nodes={21: 212 * MB, 23: 165 * MB})
    reason = _reason(_assess(activity, addresses=["5.115.21.4"]), "nodes_at_once")
    assert reason["count"] == 2
    assert reason["items"] == ["21: 212MB", "23: 165MB"]


def test_nothing_is_said_when_there_was_nothing_simultaneous():
    activity = NodeActivity(touched={21}, used={21}, concurrent=1, concurrent_nodes={21: 40 * MB})
    assert _reason(_assess(activity, addresses=["5.115.21.4"]), "nodes_at_once") is None
