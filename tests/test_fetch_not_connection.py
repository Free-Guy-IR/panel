"""Hardware ids are counted by phone model, not one-per-id.

One phone imported into two apps reports two hardware ids but one model, and
is one device. Two different phones report two models, and are two. An id whose
app reports no model (V2Box sends none) cannot be told apart from a phone
already seen, so it never adds a device on its own - it only ensures a user
with any device at all counts as at least one.
"""

from types import SimpleNamespace

import pytest

from app.models.settings import ConnectionLimit
from app.utils.connection_limiter import NodeActivity, _networks, assess

USER = SimpleNamespace(id=7, username="someone")
NOW = 1_800_000_000
HWIDS = {"284ba6ae19377354", "F9A88EE5-E2E9-4379-9860-71AE118AED7B", "9E4ACBFC-A41D-419B"}
CDN = "162.158.111.213"


def _assess(live, hwids, *, cdn=False, **overrides):
    settings = ConnectionLimit(cdn_ranges=["162.158.0.0/15"] if cdn else [], **overrides)
    return assess(
        USER,
        live,
        {},
        set(),
        NodeActivity(),
        set(),
        frozenset(),
        _networks(settings.cdn_ranges),
        {"apps": set(), "hwids": set(hwids)},
        {"284ba6ae19377354": "iPhone14,5 iOS"},
        settings,
        None,
        0,
    )


def _reason(obs, code):
    return next((r for r in obs.reasons if r.get("code") == code), None)


def test_three_ids_one_model_one_connection_is_one_device():
    """The reported case: one phone in three apps, one live connection."""
    obs = _assess({"5.115.21.4": NOW}, HWIDS)
    assert obs.address_sources == 1
    assert obs.hwid_count == 3
    assert obs.hwid_devices == 1
    assert obs.devices == 1


def test_ids_with_no_connection_still_count_at_least_one():
    """Holding the config on a phone is one device, even with nothing live now."""
    obs = _assess({}, HWIDS)
    assert obs.hwid_devices == 1
    assert obs.devices == 1
    assert obs.verdict in ("within_limit", "at_limit")


def test_two_different_models_are_two_devices():
    obs = _assess(
        {},
        {"a-one", "b-two"},
    )
    # Give each id a distinct model via a fresh assess with a devices map.
    obs = assess(
        USER, {}, {}, set(), NodeActivity(), set(), frozenset(), [],
        {"apps": set(), "hwids": {"a-one", "b-two"}},
        {"a-one": "iPhone14,5 iOS", "b-two": "SM-S911 Android"},
        ConnectionLimit(cdn_ranges=[]),
    )
    assert obs.hwid_devices == 2
    assert obs.devices == 2


def test_behind_a_cdn_the_models_are_the_signal():
    """Addresses collapse to one there, so the phones behind the ids are the floor."""
    obs = _assess({CDN: NOW}, HWIDS, cdn=True)
    assert obs.address_sources == 1
    # One known model among the three ids; the unknown ones do not add.
    assert obs.devices == 1


def test_the_strict_setting_counts_every_id():
    """count_fetched_devices treats each hardware id as its own device."""
    obs = _assess({"5.115.21.4": NOW}, HWIDS, count_fetched_devices=True)
    assert obs.devices == 3


def test_connections_still_win_when_there_are_more_of_them():
    obs = _assess({"5.115.21.4": NOW, "83.121.230.9": NOW, "2.179.8.1": NOW}, {"284ba6ae19377354"})
    assert obs.devices == 3


def test_the_evidence_shows_how_ids_map_to_phones():
    obs = _assess({"5.115.21.4": NOW}, HWIDS)
    by_model = _reason(obs, "hwid_by_model")
    assert by_model["count"] == 1
    assert by_model["items"] == ["iphone14,5 ios"]


@pytest.mark.parametrize("hwid", sorted(HWIDS))
def test_every_hardware_id_is_shown_in_full(hwid):
    """The id is what identifies a device; the model name alone is not."""
    obs = _assess({"5.115.21.4": NOW}, HWIDS)
    items = _reason(obs, "hardware_ids")["items"]
    assert any(hwid in item for item in items)


def test_a_known_model_is_shown_beside_its_id_not_instead_of_it():
    obs = _assess({"5.115.21.4": NOW}, {"284ba6ae19377354"})
    assert _reason(obs, "hardware_ids")["items"] == ["iPhone14,5 iOS - 284ba6ae19377354"]


def test_two_ids_same_model_is_one_phone():
    """The reported HQmcxpPE case: same phone in two apps - two ids, one device."""
    obs = assess(
        USER,
        {"5.115.21.4": NOW},
        {},
        set(),
        NodeActivity(),
        set(),
        frozenset(),
        [],
        {"apps": {"Happ", "V2Box"}, "hwids": {"aaa111", "bbb222"}},
        {"aaa111": "iPhone 16 Pro Max iOS", "bbb222": "iPhone 16 Pro Max iOS"},
        ConnectionLimit(cdn_ranges=[]),
    )
    # Both ids are still listed in the detail...
    assert len(_reason(obs, "hardware_ids")["items"]) == 2
    # ...but they are one phone, so one device.
    assert obs.hwid_devices == 1
    assert obs.devices == 1
