"""A carrier pool is one place, however many of its addresses a phone uses.

An Iranian mobile carrier gives a phone a different egress address per
connection, so one device shows several /24s alive at the same moment. The
reported case, hamed_7333593, read as four unrelated networks when the four
addresses were two carrier pools.

A pool is found from the panel's own traffic - a block many different users
are inside - rather than from a list, so it finds whichever carrier they are
on and nothing needs keeping up to date.
"""

from types import SimpleNamespace

import pytest

from app.models.settings import ConnectionLimit
from app.utils.connection_limiter import assess, carrier_pools

NOW = 1_800_000_000
USER = SimpleNamespace(id=16586, username="hamed_7333593")

# What the panel actually saw for them.
REPORTED = ["5.112.50.9", "5.114.198.4", "5.115.75.2", "5.217.37.8"]


def _settings(**overrides) -> ConnectionLimit:
    return ConnectionLimit(**{"cdn_ranges": [], **overrides})


def _assess(addresses, pools, settings=None):
    settings = settings or _settings()
    return assess(
        USER,
        {ip: NOW for ip in addresses},
        set(),
        {},
        set(),
        pools,
        [],
        {"apps": set(), "hwids": set()},
        {},
        settings,
        None,
        0,
    )


def _reason(obs, code):
    return next((r for r in obs.reasons if r.get("code") == code), None)


# ------------------------------------------------------- finding the pools --

def test_a_block_many_users_are_inside_is_a_pool():
    """84 of this panel's users were inside 5.112.0.0/12."""
    live = {user_id: {f"5.112.{user_id}.1": NOW} for user_id in range(30)}
    assert carrier_pools(live, _settings(carrier_pool_min_users=10)) == frozenset({"5.112.0.0/12"})


def test_a_block_one_household_uses_is_not():
    live = {1: {"83.121.230.4": NOW, "83.121.230.9": NOW}}
    assert carrier_pools(live, _settings(carrier_pool_min_users=10)) == frozenset()


def test_the_detection_can_be_switched_off():
    live = {user_id: {f"5.112.{user_id}.1": NOW} for user_id in range(30)}
    assert carrier_pools(live, _settings(carrier_pool_min_users=0)) == frozenset()


# ------------------------------------------------------------- the effect --

def test_the_reported_case_reads_as_two_places_not_four():
    obs = _assess(REPORTED, frozenset({"5.112.0.0/12", "5.208.0.0/12"}))
    assert obs.address_sources == 2
    assert obs.devices == 2


def test_without_the_pools_it_reads_as_four():
    """Which is what was happening, and what the report was about."""
    obs = _assess(REPORTED, frozenset())
    assert obs.address_sources == 4


def test_the_evidence_says_the_addresses_were_pooled():
    obs = _assess(REPORTED, frozenset({"5.112.0.0/12", "5.208.0.0/12"}))
    reason = _reason(obs, "carrier_pool")
    assert reason["count"] == 4
    assert sorted(reason["items"]) == ["5.112.0.0/12", "5.208.0.0/12"]


def test_pooled_addresses_are_not_counted_as_unrelated_networks():
    obs = _assess(REPORTED, frozenset({"5.112.0.0/12", "5.208.0.0/12"}))
    unrelated = _reason(obs, "unrelated_networks")
    assert unrelated is None or unrelated["count"] == 2


def test_a_home_address_beside_a_pool_still_counts_separately():
    """Collapsing the pool must not swallow a genuinely different place."""
    obs = _assess(["5.112.50.9", "5.114.198.4", "83.121.230.4"], frozenset({"5.112.0.0/12"}))
    assert obs.address_sources == 2


@pytest.mark.parametrize("addresses", [REPORTED[:1], REPORTED[:2], REPORTED])
def test_however_many_of_one_pool_are_seen_it_stays_one_place(addresses):
    obs = _assess(addresses, frozenset({"5.112.0.0/12", "5.208.0.0/12"}))
    assert obs.address_sources <= 2
