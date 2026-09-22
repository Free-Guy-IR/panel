from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db.crud.node import clear_usage_data
from app.db.models import NodeUsage
from app.models.node import UsageTable
from tests.api import GetTestDB

TEHRAN = timezone(timedelta(hours=3, minutes=30))


async def _seed(db, node_id: int, moments: list[datetime]) -> None:
    for moment in moments:
        db.add(NodeUsage(node_id=node_id, created_at=moment, uplink=1, downlink=1))
    await db.commit()


async def _remaining(db, node_id: int) -> list[datetime]:
    rows = await db.execute(select(NodeUsage.created_at).where(NodeUsage.node_id == node_id).order_by(NodeUsage.created_at))
    return [r[0] for r in rows.all()]


@pytest.mark.asyncio
async def test_offset_aware_start_is_converted_not_relabelled():
    node_id = 990001
    kept = datetime(2026, 4, 1, 18, 0, 0)
    deleted = datetime(2026, 4, 1, 21, 0, 0)

    async with GetTestDB() as db:
        await _seed(db, node_id, [kept, deleted])
        await clear_usage_data(db, UsageTable.node_usages, start=datetime(2026, 4, 2, 0, 0, 0, tzinfo=TEHRAN))
        left = await _remaining(db, node_id)

    assert left == [kept]


@pytest.mark.asyncio
async def test_offset_aware_end_is_converted_not_relabelled():
    node_id = 990002
    deleted = datetime(2026, 4, 1, 18, 0, 0)
    kept = datetime(2026, 4, 1, 21, 0, 0)

    async with GetTestDB() as db:
        await _seed(db, node_id, [deleted, kept])
        await clear_usage_data(db, UsageTable.node_usages, end=datetime(2026, 4, 2, 0, 0, 0, tzinfo=TEHRAN))
        left = await _remaining(db, node_id)

    assert left == [kept]


@pytest.mark.asyncio
async def test_naive_input_still_treated_as_utc():
    node_id = 990003
    kept = datetime(2026, 4, 1, 18, 0, 0)
    deleted = datetime(2026, 4, 1, 21, 0, 0)

    async with GetTestDB() as db:
        await _seed(db, node_id, [kept, deleted])
        await clear_usage_data(db, UsageTable.node_usages, start=datetime(2026, 4, 1, 20, 0, 0))
        left = await _remaining(db, node_id)

    assert left == [kept]


@pytest.mark.asyncio
async def test_utc_aware_input_is_unchanged():
    node_id = 990004
    kept = datetime(2026, 4, 1, 18, 0, 0)
    deleted = datetime(2026, 4, 1, 21, 0, 0)

    async with GetTestDB() as db:
        await _seed(db, node_id, [kept, deleted])
        await clear_usage_data(db, UsageTable.node_usages, start=datetime(2026, 4, 1, 20, 0, 0, tzinfo=UTC))
        left = await _remaining(db, node_id)

    assert left == [kept]
