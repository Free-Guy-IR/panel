from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.models.stats import Period
from app.models.user import UsersUsageBreakdownQuery
from app.operation import OperatorType
from app.operation.user import UserOperation

START = datetime(2026, 9, 1, tzinfo=UTC)
END = datetime(2026, 9, 2, tzinfo=UTC)


class WindowRejected(Exception):
    pass


def _operator() -> UserOperation:
    return UserOperation(operator_type=OperatorType.API)


@pytest.mark.parametrize(
    "extra",
    [{"group_by_admin": True}, {"core_id": 7}, {"core_id": 7, "group_by_admin": True}],
)
@pytest.mark.asyncio
async def test_breakdown_parameters_cannot_skip_the_date_window_validation(extra):
    operator = _operator()
    query = UsersUsageBreakdownQuery(start=START, end=END, period=Period.hour, **extra)

    with (
        patch.object(UserOperation, "validate_dates", AsyncMock(side_effect=WindowRejected())) as validate,
        patch("app.operation.user.get_all_users_usages", new_callable=AsyncMock) as aggregate,
        pytest.raises(WindowRejected),
    ):
        await operator.get_users_usage(AsyncMock(), SimpleNamespace(), query)

    validate.assert_awaited_once_with(START, END, True, period=Period.hour)
    aggregate.assert_not_called()


@pytest.mark.asyncio
async def test_grouping_by_node_and_by_admin_together_is_rejected():
    operator = _operator()
    query = UsersUsageBreakdownQuery(start=START, end=END, group_by_node=True, group_by_admin=True)

    with (
        patch.object(UserOperation, "validate_dates", AsyncMock(return_value=(START, END))),
        patch("app.operation.user.get_all_users_usages", new_callable=AsyncMock) as aggregate,
        pytest.raises(HTTPException) as raised,
    ):
        await operator.get_users_usage(AsyncMock(), SimpleNamespace(), query)

    assert raised.value.status_code == 400
    aggregate.assert_not_called()


@pytest.mark.asyncio
async def test_core_filter_is_dropped_without_the_node_stats_permission():
    operator = _operator()
    query = UsersUsageBreakdownQuery(start=START, end=END, core_id=7, node_id=3, group_by_admin=True)

    with (
        patch.object(UserOperation, "validate_dates", AsyncMock(return_value=(START, END))),
        patch("app.operation.user._has_permission", return_value=False),
        patch("app.operation.user._resolve_users_usage_admins_filter", AsyncMock(return_value=["self"])),
        patch("app.operation.user.get_all_users_usages", new_callable=AsyncMock) as aggregate,
    ):
        await operator.get_users_usage(AsyncMock(), SimpleNamespace(id=1, is_owner=True, role=None), query)

    kwargs = aggregate.await_args.kwargs
    assert kwargs["core_id"] is None
    assert kwargs["node_id"] is None
    assert kwargs["group_by_admin"] is True
    assert kwargs["admins"] == ["self"]
