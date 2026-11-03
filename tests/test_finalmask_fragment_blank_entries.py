import json

import pytest
from pydantic import ValidationError

from app.core.hosts import _normalize_finalmask_link
from app.models.host import FinalMask
from app.subscription.base import normalize_and_remove_none_values
from app.subscription.xray import XrayConfiguration


def _stored(lengths, delays) -> dict:
    settings = {"packets": "tlshello"}
    if lengths is not None:
        settings["lengths"] = lengths
    if delays is not None:
        settings["delays"] = delays
    return {"tcp": [{"type": "fragment", "settings": settings}]}


def _link(value) -> dict | None:
    emitted = _normalize_finalmask_link(value)
    return None if emitted is None else json.loads(emitted)


def _xray(value) -> dict | None:
    return XrayConfiguration._stream_setting_config(finalmask=value).get("finalmask")


def _fragment_settings(finalmask: dict | None) -> dict:
    if not finalmask:
        return {}
    layer = finalmask["tcp"][0]
    return layer.get("settings") or {}


def _build(build, lengths, delays):
    try:
        return build(_stored(lengths, delays))
    except ValidationError:
        pytest.skip("FinalMask rejects None list entries, so no model can carry them")


INPUT_BUILDERS = [
    pytest.param(lambda stored: stored, id="dict"),
    pytest.param(lambda stored: json.dumps(stored), id="json-string"),
    pytest.param(lambda stored: FinalMask.model_validate(stored), id="model"),
]

BLANK_ENTRY_CASES = [
    pytest.param(["", "100-200"], ["10-20"], "100-200", "10-20", id="lengths-empty-string-first"),
    pytest.param([None, "100-200"], ["10-20"], "100-200", "10-20", id="lengths-none-first"),
    pytest.param(["  ", "100-200"], ["10-20"], "100-200", "10-20", id="lengths-whitespace-first"),
    pytest.param(["100-200"], ["", "10-20"], "100-200", "10-20", id="delays-empty-string-first"),
    pytest.param(["100-200"], [None, "10-20"], "100-200", "10-20", id="delays-none-first"),
    pytest.param(["100-200"], ["  ", "10-20"], "100-200", "10-20", id="delays-whitespace-first"),
    pytest.param(["", None, "100-200", "", "200-300"], [None, "", "10-20"], "100-200", "10-20", id="mixed"),
]


@pytest.mark.parametrize("build", INPUT_BUILDERS)
@pytest.mark.parametrize("lengths,delays,length,delay", BLANK_ENTRY_CASES)
def test_blank_fragment_entries_do_not_hide_the_scalar_on_either_path(build, lengths, delays, length, delay):
    link_value = _build(build, lengths, delays)
    xray_value = _stored(lengths, delays) if isinstance(link_value, str) else _build(build, lengths, delays)

    link = _fragment_settings(_link(link_value))
    xray = _fragment_settings(_xray(xray_value))

    for settings in (link, xray):
        assert settings.get("length") == length
        assert settings.get("delay") == delay
        assert all(entry not in (None, "") and str(entry).strip() for entry in settings["lengths"])
        assert all(entry not in (None, "") and str(entry).strip() for entry in settings["delays"])
        assert settings["lengths"][0] == settings["length"]
        assert settings["delays"][0] == settings["delay"]
    assert link == xray


@pytest.mark.parametrize("build", INPUT_BUILDERS)
@pytest.mark.parametrize(
    "lengths,delays",
    [
        pytest.param(["", ""], [""], id="all-empty-strings"),
        pytest.param([None, "  "], [None], id="none-and-whitespace"),
    ],
)
def test_all_blank_fragment_lists_emit_no_scalar_on_either_path(build, lengths, delays):
    link_value = _build(build, lengths, delays)
    xray_value = _stored(lengths, delays) if isinstance(link_value, str) else _build(build, lengths, delays)

    link = _fragment_settings(_link(link_value))
    xray = _fragment_settings(_xray(xray_value))

    for settings in (link, xray):
        assert "length" not in settings
        assert "delay" not in settings
        assert not settings.get("lengths")
        assert not settings.get("delays")
    assert link == xray


def test_blank_entries_are_pruned_without_mutating_the_caller_input():
    stored = _stored(["", "100-200"], [None, "10-20"])
    snapshot = json.dumps(stored, sort_keys=True)

    _normalize_finalmask_link(stored)
    XrayConfiguration._stream_setting_config(finalmask=stored)

    assert json.dumps(stored, sort_keys=True) == snapshot


@pytest.mark.parametrize("build", INPUT_BUILDERS)
@pytest.mark.parametrize(
    "lengths,delays,scalar_key",
    [
        pytest.param(["100-200"], [0], "delay", id="delays-int-zero"),
        pytest.param([0], ["10-20"], "length", id="lengths-int-zero"),
    ],
)
def test_zero_first_entry_scalar_is_dropped_on_both_served_paths(build, lengths, delays, scalar_key):
    link_value = _build(build, lengths, delays)
    xray_value = _stored(lengths, delays) if isinstance(link_value, str) else _build(build, lengths, delays)

    link = _fragment_settings(_link(link_value))
    xray = _fragment_settings(normalize_and_remove_none_values({"finalmask": _xray(xray_value)})["finalmask"])

    for settings in (link, xray):
        assert settings["lengths"] == lengths
        assert settings["delays"] == delays
        assert scalar_key not in settings
    assert link == xray
