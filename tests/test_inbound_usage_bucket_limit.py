from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException, status

import app.fork.operation.node_extras as node_extras_operation
from app.models.admin import AdminDetails, AdminRoleData
from app.models.node import InboundUsageQuery
from app.models.stats import MAX_USAGE_BUCKETS, Period
from app.operation import OperatorType
from app.operation.node import NodeOperation

END = datetime(2026, 11, 14, 12, tzinfo=UTC)
OWNER = AdminDetails(id=1, username="owner", role=AdminRoleData(is_owner=True))


def _query(period: Period, days: int) -> InboundUsageQuery:
    return InboundUsageQuery(period=period, start=END - timedelta(days=days), end=END)


@pytest.fixture
def fetched(monkeypatch):
    calls = []

    async def record_fetch(_db, start, end, **kwargs):
        calls.append((start, end, kwargs["period"]))
        return "stats"

    monkeypatch.setattr(node_extras_operation, "fetch_inbounds_usage", record_fetch)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("period", "days"),
    [(Period.minute, 5), (Period.minute, 7), (Period.minute, 34), (Period.hour, 7), (Period.hour, 208)],
)
async def test_inbound_usage_counts_minute_buckets_per_ten_minute_usage_row(fetched, period, days):
    result = await NodeOperation(OperatorType.API).get_inbounds_usage(None, _query(period, days), admin=OWNER)

    assert result == "stats"
    assert fetched == [(END - timedelta(days=days), END, period)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("period", "days", "buckets"),
    [(Period.minute, 35, 5040), (Period.minute, 60, 8640), (Period.hour, 379, 9096)],
)
async def test_inbound_usage_still_refuses_a_range_past_the_bucket_limit(fetched, period, days, buckets):
    with pytest.raises(HTTPException) as refused:
        await NodeOperation(OperatorType.API).get_inbounds_usage(None, _query(period, days), admin=OWNER)

    assert refused.value.status_code == status.HTTP_400_BAD_REQUEST
    assert f"it needs {buckets} buckets, the limit is {MAX_USAGE_BUCKETS}" in refused.value.detail
    assert fetched == []
