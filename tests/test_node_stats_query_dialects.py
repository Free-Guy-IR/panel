from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.db.crud.node import get_node_stats
from app.db.models import Node, NodeStat
from app.models.stats import Period
from tests import test_record_usages as recorder_tests

session_factory = recorder_tests.session_factory

TEHRAN = timezone(timedelta(hours=3, minutes=30))


async def _seed(session_factory) -> int:
    async with session_factory() as session:
        node = Node(
            name="stats",
            address="10.0.0.9",
            port=1009,
            api_port=1010,
            server_ca="ca9",
            api_key="key9",
            core_config_id=None,
        )
        session.add(node)
        await session.flush()
        node_id = node.id
        for minute, cpu in ((40, 10.0), (70, 30.0), (110, 50.0)):
            stat = NodeStat(
                node_id=node_id,
                mem_total=100,
                mem_used=50,
                cpu_cores=2,
                cpu_usage=cpu,
                incoming_bandwidth_speed=1,
                outgoing_bandwidth_speed=2,
            )
            stat.created_at = datetime(2026, 10, 1, 6, 0, tzinfo=UTC) + timedelta(minutes=minute)
            session.add(stat)
        await session.commit()
    return node_id


@pytest.mark.asyncio
async def test_node_stats_with_an_unaligned_timezone_aware_start_run_on_every_database(session_factory):
    node_id = await _seed(session_factory)
    start = datetime(2026, 10, 1, 10, 2, 37, tzinfo=TEHRAN)
    end = datetime(2026, 10, 1, 12, 0, tzinfo=TEHRAN)

    async with session_factory() as session:
        result = await get_node_stats(session, node_id, start, end, Period.hour)

    assert [(stat.period_start, round(stat.cpu_usage_percentage, 3)) for stat in result.stats] == [
        (datetime(2026, 10, 1, 10, 0, tzinfo=TEHRAN), 20.0),
        (datetime(2026, 10, 1, 11, 0, tzinfo=TEHRAN), 50.0),
    ]
