import pytest
from fastapi import HTTPException

from app.routers import node as node_router


class _Admin:
    def __init__(self, is_owner):
        self.is_owner = is_owner
        self.username = "someone"


@pytest.mark.asyncio
async def test_a_non_owner_with_the_logs_permission_cannot_read_node_logs(monkeypatch: pytest.MonkeyPatch):
    async def permitted(request, db, token, resource, action):
        assert (resource, action) == ("nodes", "logs")
        return _Admin(is_owner=False)

    async def handler(node_id, request):
        raise AssertionError("the stream must not be reached by a non-owner")

    monkeypatch.setattr(node_router, "require_permission_for_request", permitted)
    monkeypatch.setattr(node_router, "_node_logs_handler", handler)

    with pytest.raises(HTTPException) as refused:
        await node_router.node_logs(node_id=1, request=object(), token="t")

    assert refused.value.status_code == 403
    assert refused.value.detail == node_router.NODE_LOGS_OWNER_MESSAGE


@pytest.mark.asyncio
async def test_an_owner_still_reads_node_logs(monkeypatch: pytest.MonkeyPatch):
    reached = {}

    async def permitted(request, db, token, resource, action):
        return _Admin(is_owner=True)

    async def handler(node_id, request):
        reached["node_id"] = node_id
        return "stream"

    monkeypatch.setattr(node_router, "require_permission_for_request", permitted)
    monkeypatch.setattr(node_router, "_node_logs_handler", handler)

    assert await node_router.node_logs(node_id=11, request=object(), token="t") == "stream"
    assert reached["node_id"] == 11
