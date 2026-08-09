"""Holding the configuration is not the same as being connected.

Fetching the subscription link tells us a device has the configuration. It
does not tell us the device is connected - an app refreshes in the background
with the tunnel off, a link imported on a new phone is fetched once and never
used, and a link handed to someone else is fetched by them too.

Behind a CDN it is the only signal left, because every address collapses into
one there, and that is the case it was brought in for.
"""

from types import SimpleNamespace

import pytest

from app.models.settings import ConnectionLimit
from app.utils.connection_limiter import _networks, assess

USER = SimpleNamespace(id=7, username="someone")
NOW = 1_800_000_000
HWIDS = {"284ba6ae19377354", "F9A88EE5-E2E9-4379-9860-71AE118AED7B", "9E4ACBFC-A41D-419B"}
CDN = "162.158.111.213"


def _assess(live, hwids, *, cdn=False, **overrides):
    settings = ConnectionLimit(cdn_ranges=["162.158.0.0/15"] if cdn else [], **overrides)
    return assess(
        USER,
        live,
        set(),
        {},
        set(),
        _networks(settings.cdn_ranges),
        {"apps": set(), "hwids": set(hwids)},
        {"284ba6ae19377354": "iPhone14,5 iOS"},
        settings,
        None,
        0,
    )


def _reason(obs, code):
    return next((r for r in obs.reasons if r.get("code") == code), None)


def test_three_fetches_and_one_connection_is_one_device():
    """The reported case: fetches were standing in for connections."""
    obs = _assess({"5.115.21.4": NOW}, HWIDS)
    assert obs.address_sources == 1
    assert obs.hwid_count == 3
    assert obs.devices == 1


def test_a_fetch_with_no_connection_at_all_is_no_device():
    obs = _assess({}, HWIDS)
    assert obs.devices == 0
    assert obs.verdict == "within_limit"


def test_behind_a_cdn_the_fetches_are_still_the_only_signal():
    """Every address collapses to one there, so the fetch count is the floor."""
    obs = _assess({CDN: NOW}, HWIDS, cdn=True)
    assert obs.address_sources == 1
    assert obs.devices == 3


def test_the_operator_can_ask_for_the_old_behaviour():
    obs = _assess({"5.115.21.4": NOW}, HWIDS, count_fetched_devices=True)
    assert obs.devices == 3


def test_connections_still_win_when_there_are_more_of_them():
    obs = _assess({"5.115.21.4": NOW, "83.121.230.9": NOW, "2.179.8.1": NOW}, {"284ba6ae19377354"})
    assert obs.devices == 3


def test_the_evidence_says_the_fetches_were_seen_and_not_counted():
    obs = _assess({"5.115.21.4": NOW}, HWIDS)
    assert _reason(obs, "fetched_not_counted") == {"code": "fetched_not_counted", "count": 3}


def test_the_evidence_does_not_say_that_when_they_were_counted():
    obs = _assess({CDN: NOW}, HWIDS, cdn=True)
    assert _reason(obs, "fetched_not_counted") is None


@pytest.mark.parametrize("hwid", sorted(HWIDS))
def test_every_hardware_id_is_shown_in_full(hwid):
    """The id is what identifies a device; the model name alone is not."""
    obs = _assess({"5.115.21.4": NOW}, HWIDS)
    items = _reason(obs, "hardware_ids")["items"]
    assert any(hwid in item for item in items)


def test_a_known_model_is_shown_beside_its_id_not_instead_of_it():
    obs = _assess({"5.115.21.4": NOW}, {"284ba6ae19377354"})
    assert _reason(obs, "hardware_ids")["items"] == ["iPhone14,5 iOS - 284ba6ae19377354"]


def test_two_devices_of_the_same_model_stay_two():
    """Keyed by the id, so identical model names cannot collapse into one."""
    obs = assess(
        USER,
        {"5.115.21.4": NOW},
        set(),
        {},
        set(),
        [],
        {"apps": set(), "hwids": {"aaa111", "bbb222"}},
        {"aaa111": "iPhone14,5 iOS", "bbb222": "iPhone14,5 iOS"},
        ConnectionLimit(cdn_ranges=[]),
        None,
        0,
    )
    assert len(_reason(obs, "hardware_ids")["items"]) == 2
