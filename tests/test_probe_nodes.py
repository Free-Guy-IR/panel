"""A server that was merely tried is not a server the user is on.

A client that tries every server completes a handshake on each and moves on.
On this panel one such user left 29.6 MB on the node they were really using
and between 5 and 35 KB on ten others, which read as being on eleven nodes.

Traffic separates the two. What must not follow is asking fewer nodes for the
user's live addresses - a node that only saw a handshake can still be holding
one, and dropping it would undercount the devices.
"""

import pytest

from app.models.settings import ConnectionLimit
from app.utils.connection_limiter import nodes_actually_used

# The real reading, in bytes.
PROBED = {21: 29_625_171, 40: 35_080, 30: 35_074, 24: 11_692, 37: 11_644,
          23: 8_654, 27: 7_146, 22: 5_846, 16: 5_845, 26: 5_845, 29: 5_845}


def test_the_one_node_actually_used_is_the_one_reported():
    assert nodes_actually_used(PROBED, ConnectionLimit()) == {21}


def test_two_nodes_genuinely_carrying_traffic_both_count():
    both = {21: 29_625_171, 40: 8_400_000}
    assert nodes_actually_used(both, ConnectionLimit()) == {21, 40}


def test_a_threshold_of_zero_counts_every_node_touched():
    """The old behaviour is still reachable for anyone who wants it."""
    assert nodes_actually_used(PROBED, ConnectionLimit(node_min_traffic_kb=0)) == set(PROBED)


@pytest.mark.parametrize("kb, expected", [(1, 11), (10, 5), (100, 1), (1024, 1)])
def test_the_threshold_is_the_setting(kb, expected):
    assert len(nodes_actually_used(PROBED, ConnectionLimit(node_min_traffic_kb=kb))) == expected


def test_exactly_at_the_threshold_counts():
    assert nodes_actually_used({1: 1024 * 1024}, ConnectionLimit()) == {1}


def test_one_byte_short_does_not():
    assert nodes_actually_used({1: 1024 * 1024 - 1}, ConnectionLimit()) == set()


def test_no_traffic_at_all_is_no_nodes():
    assert nodes_actually_used({}, ConnectionLimit()) == set()
