"""Regression tests for blank FinalMask fields reaching the client config.

A cleared optional input in the dashboard is stored as an empty string rather
than null. model_dump(exclude_none=True) only removes null, so the blank used
to survive into streamSettings.finalmask, and Xray reads a present
`"maxSplit": ""` as 0 - rejecting the entire config with

    failed to build mask with type fragment: Last lengths entry min can't be 0

That breaks every client using the host, not just the field that was cleared,
so it is worth pinning down.
"""

from app.models.host import FinalMask, prune_blank_values, to_xray_finalmask


def test_blank_max_split_is_not_emitted():
    """The exact shape a host with a cleared Max Split is stored as."""
    stored = {
        "tcp": [
            {
                "type": "fragment",
                "settings": {
                    "packets": "tlshello",
                    "lengths": ["6-9"],
                    "delays": ["1-2"],
                    "max_split": "",
                },
            }
        ],
        "udp": None,
        "quic_params": {"congestion": "reno"},
    }

    dumped = FinalMask(**stored).model_dump(exclude_none=True, by_alias=True, mode="json")
    emitted = prune_blank_values(dumped)

    settings = emitted["tcp"][0]["settings"]
    assert "maxSplit" not in settings
    assert settings["packets"] == "tlshello"
    assert settings["lengths"] == ["6-9"]
    assert settings["delays"] == ["1-2"]


def test_meaningful_falsy_values_are_kept():
    """0 and False are real settings, unlike "" - they must not be dropped."""
    assert prune_blank_values({"id": 0}) == {"id": 0}
    assert prune_blank_values({"dgram": False}) == {"dgram": False}


def test_blank_containers_and_strings_are_dropped():
    assert prune_blank_values({"a": "", "b": "keep"}) == {"b": "keep"}
    assert prune_blank_values({"a": "   ", "b": "keep"}) == {"b": "keep"}
    assert prune_blank_values({"a": [], "b": "keep"}) == {"b": "keep"}
    assert prune_blank_values({"a": {}, "b": "keep"}) == {"b": "keep"}


def test_nested_and_list_pruning():
    assert prune_blank_values({"s": {"x": "", "y": 1}}) == {"s": {"y": 1}}
    assert prune_blank_values({"l": [{"a": ""}, {"b": 2}]}) == {"l": [{"b": 2}]}


def test_fully_blank_prunes_to_none():
    """An all-blank finalmask must collapse so the key can be omitted entirely."""
    assert prune_blank_values({"a": "", "b": {}, "c": []}) is None


def _emitted(stored: dict) -> dict:
    dumped = FinalMask(**stored).model_dump(exclude_none=True, by_alias=True, mode="json")
    return to_xray_finalmask(dumped)["tcp"][0]["settings"]


def test_several_lengths_collapse_to_the_span_they_cover():
    settings = _emitted(
        {
            "tcp": [
                {
                    "type": "fragment",
                    "settings": {"packets": "tlshello", "lengths": ["3-5", "6-8", "10-20"], "delays": [1, 2]},
                }
            ]
        }
    )

    assert settings["length"] == "3-20"
    assert settings["delay"] == "1-2"
    assert "lengths" not in settings and "delays" not in settings


def test_a_single_length_is_carried_over_exactly():
    settings = _emitted(
        {"tcp": [{"type": "fragment", "settings": {"packets": "tlshello", "lengths": ["24-70"], "delays": ["0"]}}]}
    )

    assert settings["length"] == "24-70"
    assert settings["delay"] == "0"


def test_the_model_itself_keeps_the_shape_the_api_returns():
    stored = {"tcp": [{"type": "fragment", "settings": {"packets": "tlshello", "lengths": ["24-70"], "delays": ["0"]}}]}

    settings = FinalMask(**stored).model_dump(exclude_none=True, by_alias=True, mode="json")["tcp"][0]["settings"]

    assert settings["lengths"] == ["24-70"]
    assert settings["delays"] == ["0"]
    assert "length" not in settings and "delay" not in settings


def test_a_layer_without_fragment_lists_passes_through():
    stored = {"tcp": [{"type": "sudoku", "settings": {"packets": "tlshello"}}], "udp": []}

    emitted = to_xray_finalmask(FinalMask(**stored).model_dump(exclude_none=True, by_alias=True, mode="json"))

    assert emitted["tcp"][0]["settings"] == {"packets": "tlshello"}
