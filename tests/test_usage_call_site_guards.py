import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, status

import app.fork.operation.node_extras as node_extras_operation
import app.operation.admin as admin_operation
import app.operation.node as node_operation
import app.operation.user as user_operation
from app.models.admin import AdminDetails, AdminRoleData
from app.models.node import InboundUsageQuery, NodeUsageQuery
from app.models.stats import Period, UserCountMetric
from app.operation import OperatorType
from app.operation.admin import AdminOperation
from app.operation.node import NodeOperation
from app.operation.user import UserOperation


def _owner(admin_id: int) -> AdminDetails:
    return AdminDetails(id=admin_id, username=f"owner{admin_id}", role=AdminRoleData(is_owner=True))


def _day_query(model):
    end = datetime.now(UTC)
    return model(start=end - timedelta(days=1), end=end, period=Period.hour)


def _sub_chart(operation: UserOperation, admin: AdminDetails):
    return operation.get_users_sub_update_chart(None, admin, period=Period.hour)


def _admin_usage(operation: AdminOperation, admin: AdminDetails):
    db_admin = SimpleNamespace(id=admin.id, username=admin.username)
    end = datetime.now(UTC)
    return operation._get_admin_usage(None, db_admin, admin, end - timedelta(days=1), end, Period.hour)


def _nodes_usage(operation: NodeOperation, admin: AdminDetails):
    return operation.get_usage(None, _day_query(NodeUsageQuery), admin=admin)


def _nodes_user_count(operation: NodeOperation, admin: AdminDetails):
    return operation.get_user_count_metric(None, UserCountMetric.online, _day_query(NodeUsageQuery), admin=admin)


def _inbounds_usage(operation: NodeOperation, admin: AdminDetails):
    return operation.get_inbounds_usage(None, _day_query(InboundUsageQuery), admin=admin)


GUARDED = {
    "users_sub_update_chart": (UserOperation, user_operation, "get_users_subscription_agent_counts", _sub_chart),
    "admin_usage": (AdminOperation, admin_operation, "get_admin_usages", _admin_usage),
    "nodes_usage": (NodeOperation, node_operation, "get_nodes_usage", _nodes_usage),
    "nodes_user_count": (NodeOperation, node_operation, "get_user_count_metric_stats", _nodes_user_count),
    "inbounds_usage": (NodeOperation, node_extras_operation, "fetch_inbounds_usage", _inbounds_usage),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", sorted(GUARDED))
async def test_a_second_aggregation_of_the_same_chart_by_the_same_admin_is_refused(monkeypatch, endpoint):
    operation_class, module, crud_name, call = GUARDED[endpoint]
    started = asyncio.Event()
    released = asyncio.Event()

    async def slow_aggregate(*_args, **_kwargs):
        started.set()
        await released.wait()
        return []

    monkeypatch.setattr(module, crud_name, slow_aggregate)
    if endpoint == "users_sub_update_chart":
        monkeypatch.setattr(module, "get_users_subscription_agent_stats", slow_aggregate)
        monkeypatch.setattr(UserOperation, "_build_user_agent_chart", classmethod(lambda cls, *a, **k: "chart"))
    operation = operation_class(OperatorType.API)

    first = asyncio.create_task(call(operation, _owner(1)))
    await asyncio.wait_for(started.wait(), timeout=5)
    with pytest.raises(HTTPException) as refused:
        await asyncio.wait_for(call(operation, _owner(1)), timeout=5)
    assert refused.value.status_code == status.HTTP_429_TOO_MANY_REQUESTS

    other_admin = asyncio.create_task(call(operation, _owner(2)))
    await asyncio.sleep(0)
    released.set()
    await first
    await other_admin
    await call(operation, _owner(1))


@pytest.mark.asyncio
async def test_the_node_usage_chart_and_the_node_user_count_chart_load_side_by_side(monkeypatch):
    started = asyncio.Event()
    released = asyncio.Event()

    async def slow_usage(*_args, **_kwargs):
        started.set()
        await released.wait()
        return "usage"

    async def quick_count(*_args, **_kwargs):
        return "count"

    monkeypatch.setattr(node_operation, "get_nodes_usage", slow_usage)
    monkeypatch.setattr(node_operation, "get_user_count_metric_stats", quick_count)
    operation = NodeOperation(OperatorType.API)

    usage = asyncio.create_task(_nodes_usage(operation, _owner(1)))
    await asyncio.wait_for(started.wait(), timeout=5)
    assert await asyncio.wait_for(_nodes_user_count(operation, _owner(1)), timeout=5) == "count"
    released.set()
    assert await usage == "usage"
