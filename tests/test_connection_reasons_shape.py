"""A page of connection states must survive rows written in either reason format.

Reasons were once English sentences built in the backend and stored as they
read; they are now codes the frontend renders in the panel's language. A row is
only rewritten when its user is next seen online, so the table holds both
shapes for as long as some users stay away - and the response model has to read
both, or a single old row anywhere in a page fails validation and takes the
whole request down with it.
"""

import pytest
from pydantic import ValidationError

from app.models.connection_limit import ConnectionStateResponse, ConnectionStatesResponse

LEGACY = ["4 device(s) estimated", "4 unrelated networks"]
STRUCTURED = [{"code": "devices", "count": 4}, {"code": "networks", "count": 4, "items": ["1.2.3.0/24"]}]


def _state(reasons):
    return {
        "user_id": 1,
        "devices": 4,
        "address_sources": 4,
        "hwid_count": 1,
        "node_count": 2,
        "streak": 3,
        "verdict": "over_limit",
        "reasons": reasons,
        "details": {},
        "limit_applied": 2,
        "checked_at": None,
        "username": "someone",
    }


@pytest.mark.parametrize(
    "reasons",
    [
        pytest.param(LEGACY, id="sentences from before the change"),
        pytest.param(STRUCTURED, id="codes"),
        pytest.param([LEGACY[0], STRUCTURED[0]], id="a row caught mid-change"),
    ],
)
def test_a_state_reads_in_either_shape(reasons):
    assert len(ConnectionStateResponse.model_validate(_state(reasons)).reasons) == len(reasons)


def test_a_page_holding_both_shapes_is_returned():
    """The list wrapper is what the endpoint returns, and where the failure showed."""
    page = ConnectionStatesResponse.model_validate(
        {
            "states": [_state(LEGACY), _state(STRUCTURED)],
            "total": 2,
            "device_limit": 2,
            "enabled": True,
            "monitor_only": True,
        }
    )
    assert len(page.states) == 2
    assert page.enabled is True


def test_a_reason_is_still_required_to_be_one_of_the_two():
    """Reading both shapes is not the same as reading anything at all."""
    with pytest.raises(ValidationError):
        ConnectionStateResponse.model_validate(_state([{"code": "devices"}, 42]))
