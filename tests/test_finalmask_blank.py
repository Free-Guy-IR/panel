"""Regression tests for blank FinalMask fields reaching the client config.

A cleared optional input in the dashboard is stored as an empty string rather
than null. model_dump(exclude_none=True) only removes null, so the blank used
to survive into streamSettings.finalmask, and Xray reads a present
`"maxSplit": ""` as 0 - rejecting the entire config with

    failed to build mask with type fragment: Last lengths entry min can't be 0

That breaks every client using the host, not just the field that was cleared,
so it is worth pinning down.
"""

from app.models.host import FinalMask, prune_blank_values


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
    # The configured values must survive untouched.
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
