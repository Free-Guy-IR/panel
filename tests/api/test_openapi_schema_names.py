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
