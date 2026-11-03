from app.db.models import NodeStatus
from tests.api import client


def _schemas() -> dict:
    return client.app.openapi()["components"]["schemas"]


def test_no_two_models_share_an_openapi_schema_name():
    disambiguated = sorted(name for name in _schemas() if "__" in name)

    assert disambiguated == []


def test_node_status_is_the_upstream_node_enum():
    schema = _schemas()["NodeStatus"]

    assert schema["type"] == "string"
    assert schema["enum"] == [member.value for member in NodeStatus]


def test_traffic_log_node_entries_keep_their_response_fields():
    nodes = _schemas()["Status"]["properties"]["nodes"]
    ref = nodes["items"]["$ref"]
    name = ref.rsplit("/", 1)[-1]

    assert name != "NodeStatus"
    assert set(_schemas()[name]["properties"]) == {
        "node_id",
        "node",
        "state",
        "since",
        "lines",
        "events",
        "dropped",
        "dropped_records",
        "dropped_live",
        "dropped_viewer",
        "stream_full",
        "restreams",
        "records",
        "last_event_at",
        "detail",
    }
