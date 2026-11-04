from unittest.mock import AsyncMock

import pytest
from fastapi import status
from sqlalchemy.exc import OperationalError

from app.fork.crud import node as fork_node_crud
from tests.api import client
from tests.api.helpers import auth_headers, unique_name
from tests.api.test_bulk_delete_entities import create_db_node, delete_db_node


class _DriverError(Exception):
    pass


@pytest.mark.parametrize("route", ["single", "bulk"])
def test_a_node_delete_that_keeps_deadlocking_answers_503_with_the_reason(access_token, monkeypatch, route):
    from app.routers import node as node_router

    monkeypatch.setattr(node_router.node_operator, "_remove_node_impl", AsyncMock())
    monkeypatch.setattr(fork_node_crud, "NODE_DELETE_BACKOFF_SECONDS", 0)

    async def always_deadlocks(node_ids):
        raise OperationalError("DELETE FROM nodes", {}, _DriverError(1213, "Deadlock found when trying to get lock"))

    real_delete = fork_node_crud.delete_nodes_in_lock_order

    async def deadlocking_delete(db, node_ids, delete_rows, attempts=None):
        return await real_delete(db, node_ids, always_deadlocks, attempts)

    node_id = create_db_node(core_config_id=1, name=unique_name("deadlocked_node"))
    try:
        from app.db.crud import node as node_crud

        monkeypatch.setattr(node_crud, "delete_nodes_in_lock_order", deadlocking_delete)
        if route == "single":
            response = client.delete(f"/api/node/{node_id}", headers=auth_headers(access_token))
        else:
            response = client.post(
                "/api/nodes/bulk/delete", headers=auth_headers(access_token), json={"ids": [node_id]}
            )
        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert f"gave up after {fork_node_crud.NODE_DELETE_ATTEMPTS} attempts" in response.json()["detail"]
    finally:
        monkeypatch.undo()
        delete_db_node(node_id)
