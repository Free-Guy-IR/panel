import json
import re
from copy import deepcopy

import pytest

from app.core.hosts import _normalize_finalmask_link
from app.models.host import FinalMask
from app.subscription.xray import XrayConfiguration

LISTS_ONLY = {
    "tcp": [
        {
            "type": "fragment",
            "settings": {
                "packets": "tlshello",
                "lengths": ["100-200", "200-300"],
                "delays": ["10-20", "20-30"],
            },
        }
    ]
}

SCALAR_AND_LISTS = {
    "tcp": [
        {
            "type": "fragment",
            "settings": {
                "packets": "tlshello",
                "length": "50-60",
                "delay": "5-6",
                "lengths": ["100-200", "200-300"],
                "delays": ["10-20", "20-30"],
            },
        }
    ]
}


def _dashboard_compat(finalmask: dict) -> dict:
    result = deepcopy(finalmask)
    for layer in result.get("tcp") or []:
        if not isinstance(layer, dict) or layer.get("type") != "fragment":
            continue
        settings = layer.get("settings")
        if not isinstance(settings, dict):
            continue
        lengths = settings.get("lengths")
        if settings.get("length") in (None, "") and isinstance(lengths, list) and lengths:
            settings["length"] = lengths[0]
        delays = settings.get("delays")
        if settings.get("delay") in (None, "") and isinstance(delays, list) and delays:
            settings["delay"] = delays[0]
    return result


def _link_settings(value) -> dict:
    return json.loads(_normalize_finalmask_link(value))["tcp"][0]["settings"]


def _xray_settings(value) -> dict:
    return XrayConfiguration._stream_setting_config(finalmask=value)["finalmask"]["tcp"][0]["settings"]


def _api_shape(stored: dict) -> dict:
    return FinalMask.model_validate(stored).model_dump(by_alias=True, exclude_none=True, mode="json")


def _assert_scalar_follows_lists(settings: dict, source: dict) -> None:
    for plural, singular in (("lengths", "length"), ("delays", "delay")):
        entries = source.get(plural)
        if not entries:
            continue
        assert settings[plural] == entries
        assert settings.get(singular) not in (None, "")
        assert isinstance(settings[singular], (str, int))
        assert settings[singular] == entries[0]


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda stored: deepcopy(stored), id="dict"),
        pytest.param(lambda stored: FinalMask.model_validate(stored), id="model"),
        pytest.param(lambda stored: json.dumps(stored), id="json-string"),
    ],
)
def test_link_fragment_carries_first_entry_scalar_next_to_unchanged_lists(build):
    source = LISTS_ONLY["tcp"][0]["settings"]

    settings = _link_settings(build(LISTS_ONLY))

    _assert_scalar_follows_lists(settings, source)


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda stored: deepcopy(stored), id="dict"),
        pytest.param(lambda stored: FinalMask.model_validate(stored), id="model"),
    ],
)
def test_xray_fragment_carries_first_entry_scalar_next_to_unchanged_lists(build):
    source = LISTS_ONLY["tcp"][0]["settings"]

    settings = _xray_settings(build(LISTS_ONLY))

    _assert_scalar_follows_lists(settings, source)


def test_stored_to_api_round_trip_keeps_lists_and_emission_adds_scalar():
    api = _api_shape(deepcopy(LISTS_ONLY))
    api_settings = api["tcp"][0]["settings"]

    assert api_settings["lengths"] == ["100-200", "200-300"]
    assert api_settings["delays"] == ["10-20", "20-30"]
    assert "length" not in api_settings and "delay" not in api_settings

    _assert_scalar_follows_lists(_link_settings(api), api_settings)
    _assert_scalar_follows_lists(_xray_settings(api), api_settings)
    _assert_scalar_follows_lists(_xray_settings(FinalMask.model_validate(api)), api_settings)


def test_explicit_scalar_survives_the_raw_link_path():
    settings = _link_settings(deepcopy(SCALAR_AND_LISTS))

    assert settings["length"] == "50-60"
    assert settings["delay"] == "5-6"
    assert settings["lengths"] == ["100-200", "200-300"]
    assert settings["delays"] == ["10-20", "20-30"]


def test_model_keeps_lists_over_a_stored_scalar_and_emission_uses_their_first_entry():
    api_settings = _api_shape(deepcopy(SCALAR_AND_LISTS))["tcp"][0]["settings"]

    assert api_settings["lengths"] == ["100-200", "200-300"]
    assert "length" not in api_settings

    for settings in (_xray_settings(deepcopy(SCALAR_AND_LISTS)), _link_settings(_api_shape(SCALAR_AND_LISTS))):
        assert settings["length"] == "100-200"
        assert settings["delay"] == "10-20"
        assert settings["lengths"] == ["100-200", "200-300"]


@pytest.mark.parametrize("stored", [LISTS_ONLY, SCALAR_AND_LISTS], ids=["lists-only", "scalar-and-lists"])
def test_subscription_paths_and_dashboard_compat_agree(stored):
    api = _api_shape(deepcopy(stored))
    dashboard = _dashboard_compat(api)["tcp"][0]["settings"]

    link = _link_settings(api)
    xray = _xray_settings(api)

    assert link == dashboard
    assert xray == dashboard
    assert set(link) == set(xray) == set(dashboard)


def test_emission_never_mutates_the_stored_value():
    stored = deepcopy(LISTS_ONLY)
    model = FinalMask.model_validate(stored)
    before_model = model.model_dump(by_alias=True, exclude_none=True, mode="json")

    _link_settings(stored)
    _xray_settings(stored)
    _link_settings(model)
    _xray_settings(model)

    assert stored == LISTS_ONLY
    assert model.model_dump(by_alias=True, exclude_none=True, mode="json") == before_model


INT32_RANGE = r"^\d+(-\d+)?$"


def _fragment(settings: dict) -> dict:
    return {"tcp": [{"type": "fragment", "settings": settings}]}


def _assert_v26_3_27_reads(settings: dict) -> None:
    for plural, singular in (("lengths", "length"), ("delays", "delay")):
        if settings.get(plural) or settings.get(singular) not in (None, ""):
            value = settings[singular]
            assert not isinstance(value, (list, dict))
            assert isinstance(value, int) or re.match(INT32_RANGE, value)


def _emissions(stored: dict) -> list[dict]:
    model = FinalMask.model_validate(deepcopy(stored))
    api = model.model_dump(by_alias=True, exclude_none=True, mode="json")
    return [
        _link_settings(api),
        _link_settings(model),
        _link_settings(json.dumps(api)),
        _xray_settings(api),
        _xray_settings(model),
    ]


OLD_STORED_SHAPES = [
    pytest.param({"length": "100-200", "delay": "10-20"}, ["100-200"], ["10-20"], "100-200", "10-20", id="a-scalar"),
    pytest.param({"length": "100-300", "delay": "10-30"}, ["100-300"], ["10-30"], "100-300", "10-30", id="a-span"),
    pytest.param({"length": "24-70", "interval": "5-10"}, ["24-70"], ["5-10"], "24-70", "5-10", id="a-interval"),
    pytest.param(
        {"lengths": ["100-200", "200-300"], "delays": ["10-20", "20-30"]},
        ["100-200", "200-300"],
        ["10-20", "20-30"],
        "100-200",
        "10-20",
        id="b-lists",
    ),
    pytest.param(
        {"length": "50-60", "delay": "5-6", "lengths": ["100-200", "200-300"], "delays": ["10-20"]},
        ["100-200", "200-300"],
        ["10-20"],
        "100-200",
        "10-20",
        id="c-both",
    ),
    pytest.param({"length": "100-200", "lengths": [], "delays": []}, ["100-200"], None, "100-200", None, id="d-empty"),
    pytest.param({"lengths": ["24-70"], "delays": ["0"]}, ["24-70"], ["0"], "24-70", "0", id="e-one"),
]


@pytest.mark.parametrize("stored, lengths, delays, length, delay", OLD_STORED_SHAPES)
def test_old_stored_fragment_shapes_round_trip_to_scalars_xray_v26_3_27_reads(stored, lengths, delays, length, delay):
    api_settings = _api_shape(_fragment({"packets": "tlshello", **stored}))["tcp"][0]["settings"]

    assert api_settings["lengths"] == lengths
    assert "length" not in api_settings and "delay" not in api_settings

    for settings in _emissions(_fragment({"packets": "tlshello", **stored})):
        assert settings["packets"] == "tlshello"
        assert settings.get("lengths") == lengths
        assert settings.get("delays") == delays
        assert settings.get("length") == length
        assert settings.get("delay") == delay
        _assert_v26_3_27_reads(settings)


def test_empty_lists_without_a_scalar_emit_no_length_information():
    for settings in _emissions(_fragment({"packets": "tlshello", "lengths": [], "delays": []})):
        assert settings == {"packets": "tlshello"}


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param(_fragment({"length": "100-300"}), id="a-scalar"),
        pytest.param(_fragment({"length": "50-60", "lengths": ["100-200"]}), id="c-both"),
        pytest.param(_fragment({"lengths": ["24-70"]}), id="e-one"),
    ],
)
def test_raw_link_payload_keeps_its_scalar_and_never_invents_lists(stored):
    source = stored["tcp"][0]["settings"]

    settings = _link_settings(deepcopy(stored))

    assert settings.get("lengths") == source.get("lengths")
    assert settings["length"] == source.get("length", (source.get("lengths") or [None])[0])
    _assert_v26_3_27_reads(settings)


def test_legacy_udp_layer_lists_pass_through_untouched():
    stored = {
        "tcp": [{"type": "fragment", "settings": {"lengths": ["100-200"]}}],
        "udp": [{"type": "salamander", "settings": {"password": "x", "lengths": ["1-2", "3-4"], "delays": ["5-6"]}}],
    }

    model = FinalMask.model_validate(deepcopy(stored))
    api = model.model_dump(by_alias=True, exclude_none=True, mode="json")
    for value in (api, model):
        link = json.loads(_normalize_finalmask_link(value))
        xray = XrayConfiguration._stream_setting_config(finalmask=value)["finalmask"]
        for emitted in (link, xray):
            assert emitted["udp"] == stored["udp"]
            assert emitted["tcp"][0]["settings"]["length"] == "100-200"
