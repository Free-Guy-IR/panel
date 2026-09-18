import pytest

from app.fork.content_filter import schemas


def test_a_bulk_payload_needs_at_least_one_node_or_endpoint():
    with pytest.raises(ValueError):
        schemas.AssignmentBulkPayload(profile_id=1, node_ids=[], inbound_tags=[])
    with pytest.raises(ValueError):
        schemas.AssignmentBulkPayload(profile_id=1, node_ids=[], inbound_tags=["  "])


def test_nodes_alone_mean_every_endpoint_on_those_nodes():
    payload = schemas.AssignmentBulkPayload(profile_id=1, node_ids=[3, 4], inbound_tags=[])
    assert payload.node_ids == [3, 4]
    assert payload.inbound_tags == []


def test_endpoints_alone_mean_the_whole_fleet():
    payload = schemas.AssignmentBulkPayload(profile_id=1, node_ids=[], inbound_tags=["in-a", " in-b "])
    assert payload.inbound_tags == ["in-a", "in-b"]


def test_a_target_reports_which_nodes_share_its_core():
    target = schemas.TargetNode(
        id=10,
        name="n",
        status="connected",
        core_config_id=1,
        routing_service=True,
        shares_core_with=[20, 30],
        inbounds=[],
    )
    assert target.shares_core_with == [20, 30]
    assert target.reason is None


@pytest.mark.asyncio
async def test_a_disabled_node_is_reported_rather_than_attempted(monkeypatch: pytest.MonkeyPatch):
    from app.fork.routers import content_filter as router

    class _Node:
        status = "disabled"
        core_config_id = 1

    class _DB:
        async def get(self, model, pk):
            return _Node()

    problem = await router._target_problem(_DB(), 16, "")
    assert problem is not None
    assert "disabled" in problem


@pytest.mark.asyncio
async def test_a_core_without_the_routing_service_is_reported(monkeypatch: pytest.MonkeyPatch):
    from app.fork.routers import content_filter as router

    class _Node:
        id = 30
        status = "connected"
        core_config_id = 1

    class _Core:
        def __init__(self):
            self.type = "xray"
            self.config = {"api": None, "inbounds": [{"tag": "in", "protocol": "vless"}]}

    async def core_of(db, core_id):
        return _Core()

    class _Empty:
        def scalars(self):
            class _S:
                def all(self_inner):
                    return []

            return _S()

    class _DB:
        async def get(self, model, pk):
            return _Node()

        async def execute(self, *args, **kwargs):
            return _Empty()

    monkeypatch.setattr(router, "get_core_config_by_id", core_of)
    problem = await router._target_problem(_DB(), 30, "in")
    assert problem is not None
    assert "RoutingService" in problem


@pytest.mark.asyncio
async def test_a_disabled_node_is_not_counted_as_a_target(monkeypatch: pytest.MonkeyPatch):
    from app.fork.content_filter import service

    class _Node:
        def __init__(self, node_id, status):
            self.id = node_id
            self.status = status

    live, dead = _Node(10, "connected"), _Node(16, "disabled")

    class _Result:
        def scalars(self):
            class _S:
                def all(self_inner):
                    return [live, dead]

            return _S()

    class _DB:
        async def execute(self, *a, **k):
            return _Result()

        async def get(self, model, pk):
            return {10: live, 16: dead}.get(pk)

    async def tags(db, node_id):
        return {"in"}

    monkeypatch.setattr(service, "node_inbound_tags", tags)

    class _Assignment:
        node_id = None
        inbound_tag = "in"

    assert await service.nodes_for_assignment(_DB(), _Assignment()) == [10]

    class _Pinned:
        node_id = 16
        inbound_tag = "in"

    assert await service.nodes_for_assignment(_DB(), _Pinned()) == []
