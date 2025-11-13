from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, call

import pytest

from app.jobs import record_usages


class DummyNode:
    def __init__(self, node_id: int, usage_coefficient: int = 1):
        self.node_id = node_id
        self._usage_coefficient = usage_coefficient

    async def get_extra(self) -> dict[str, Any]:
        return {"usage_coefficient": self._usage_coefficient}


@pytest.mark.asyncio
async def test_record_user_usages_updates_users_and_admins(monkeypatch: pytest.MonkeyPatch):
    nodes = [(1, DummyNode(1, usage_coefficient=2)), (2, DummyNode(2, usage_coefficient=1))]
    monkeypatch.setattr(record_usages.node_manager, "get_healthy_nodes", AsyncMock(return_value=nodes))

    stats_map = {
        1: [{"uid": "1", "value": 100}, {"uid": "2", "value": 50}],
        2: [{"uid": "1", "value": 75}],
    }

    async def fake_get_users_stats(node: DummyNode):
        return stats_map[node.node_id]

    monkeypatch.setattr(record_usages, "get_users_stats", fake_get_users_stats)

    safe_execute_mock = AsyncMock()
    monkeypatch.setattr(record_usages, "safe_execute", safe_execute_mock)

    admin_usage = {99: 555}
    calculate_admin_usage_mock = AsyncMock(return_value=admin_usage)
    monkeypatch.setattr(record_usages, "calculate_admin_usage", calculate_admin_usage_mock)

    record_user_stats_mock = AsyncMock()
    monkeypatch.setattr(record_usages, "record_user_stats", record_user_stats_mock)
    monkeypatch.setattr(record_usages, "DISABLE_RECORDING_NODE_USAGE", False)

    await record_usages.record_user_usages()

    expected_users_usage = [
        {"uid": 1, "value": 275},
        {"uid": 2, "value": 100},
    ]
    calculate_admin_usage_mock.assert_awaited_once()
    assert calculate_admin_usage_mock.await_args.args[0] == expected_users_usage

    assert safe_execute_mock.await_count == 2
    user_call = safe_execute_mock.await_args_list[0]
    assert user_call.args[1] == expected_users_usage

    admin_call = safe_execute_mock.await_args_list[1]
    assert admin_call.args[1] == [{"admin_id": 99, "value": 555}]

    assert record_user_stats_mock.await_count == 2
    expected_record_calls = [
        call(params=stats_map[1], node_id=1, usage_coefficient=2),
        call(params=stats_map[2], node_id=2, usage_coefficient=1),
    ]
    record_user_stats_mock.assert_has_awaits(expected_record_calls, any_order=False)


@pytest.mark.asyncio
async def test_record_user_usages_returns_when_no_usage(monkeypatch: pytest.MonkeyPatch):
    nodes = [(1, DummyNode(1))]
    monkeypatch.setattr(record_usages.node_manager, "get_healthy_nodes", AsyncMock(return_value=nodes))

    async def fake_get_users_stats(_: DummyNode):
        return []

    monkeypatch.setattr(record_usages, "get_users_stats", fake_get_users_stats)

    safe_execute_mock = AsyncMock()
    monkeypatch.setattr(record_usages, "safe_execute", safe_execute_mock)

    record_user_stats_mock = AsyncMock()
    monkeypatch.setattr(record_usages, "record_user_stats", record_user_stats_mock)

    calculate_admin_usage_mock = AsyncMock()
    monkeypatch.setattr(record_usages, "calculate_admin_usage", calculate_admin_usage_mock)

    await record_usages.record_user_usages()

    safe_execute_mock.assert_not_awaited()
    record_user_stats_mock.assert_not_awaited()
    calculate_admin_usage_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_record_node_usages_updates_totals(monkeypatch: pytest.MonkeyPatch):
    nodes = [(1, DummyNode(1)), (2, DummyNode(2))]
    monkeypatch.setattr(record_usages.node_manager, "get_healthy_nodes", AsyncMock(return_value=nodes))

    stats_map = {
        1: [{"up": 10, "down": 4}, {"up": 0, "down": 3}],
        2: [{"up": 1, "down": 1}],
    }

    async def fake_get_outbounds_stats(node: DummyNode):
        return stats_map[node.node_id]

    monkeypatch.setattr(record_usages, "get_outbounds_stats", fake_get_outbounds_stats)

    safe_execute_mock = AsyncMock()
    monkeypatch.setattr(record_usages, "safe_execute", safe_execute_mock)

    record_node_stats_mock = AsyncMock()
    monkeypatch.setattr(record_usages, "record_node_stats", record_node_stats_mock)
    monkeypatch.setattr(record_usages, "DISABLE_RECORDING_NODE_USAGE", False)

    await record_usages.record_node_usages()

    assert safe_execute_mock.await_count == 2
    node_call = safe_execute_mock.await_args_list[0]
    assert node_call.args[1] == [
        {"node_id": 1, "up": 10, "down": 7},
        {"node_id": 2, "up": 1, "down": 1},
    ]

    system_call = safe_execute_mock.await_args_list[1]
    assert len(system_call.args) == 1  # system totals baked into the statement

    expected_node_calls = [
        call(stats_map[1], 1),
        call(stats_map[2], 2),
    ]
    assert record_node_stats_mock.await_args_list == expected_node_calls


@pytest.mark.asyncio
async def test_record_node_usages_returns_when_totals_zero(monkeypatch: pytest.MonkeyPatch):
    nodes = [(1, DummyNode(1))]
    monkeypatch.setattr(record_usages.node_manager, "get_healthy_nodes", AsyncMock(return_value=nodes))

    async def fake_get_outbounds_stats(_: DummyNode):
        return [{"up": 0, "down": 0}]

    monkeypatch.setattr(record_usages, "get_outbounds_stats", fake_get_outbounds_stats)

    safe_execute_mock = AsyncMock()
    monkeypatch.setattr(record_usages, "safe_execute", safe_execute_mock)

    record_node_stats_mock = AsyncMock()
    monkeypatch.setattr(record_usages, "record_node_stats", record_node_stats_mock)

    await record_usages.record_node_usages()

    safe_execute_mock.assert_not_awaited()
    record_node_stats_mock.assert_not_awaited()
