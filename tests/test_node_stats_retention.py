from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.db.models import CoreConfig, CoreType, Node, NodeStat
from tests.api import GetTestDB


@pytest.fixture(autouse=True)
async def _empty_node_stats():
    async with GetTestDB() as db:
        await db.execute(NodeStat.__table__.delete())
        await db.commit()
    yield
    async with GetTestDB() as db:
        await db.execute(NodeStat.__table__.delete())
        await db.commit()


async def _node(db):
    node = (await db.scalars(select(Node).limit(1))).first()
    if node is not None:
        return node.id
    core = (await db.scalars(select(CoreConfig).limit(1))).first()
    if core is None:
        core = CoreConfig(name="retention-core", config={}, type=CoreType.xray)
        db.add(core)
        await db.flush()
    node = Node(
        name="retention-node",
        address="127.0.0.1",
        port=62050,
        api_port=62051,
        server_ca="ca",
        api_key=str(uuid4()),
        core_config_id=core.id,
    )
    db.add(node)
    await db.flush()
    return node.id


def _sample(node_id, age_days):
    return {
        "node_id": node_id,
        "mem_total": 1,
        "mem_used": 1,
        "cpu_cores": 1,
        "cpu_usage": 1.0,
        "incoming_bandwidth_speed": 1,
        "outgoing_bandwidth_speed": 1,
        "created_at": datetime.now(UTC) - timedelta(days=age_days),
    }


@pytest.mark.asyncio
async def test_samples_past_the_window_are_dropped_and_recent_ones_are_kept(monkeypatch: pytest.MonkeyPatch):
    from app.fork.jobs import cleanup_node_stats as job

    monkeypatch.setattr(job, "GetDB", GetTestDB)
    monkeypatch.setattr(job.job_settings, "node_stats_retention_days", 14)

    async with GetTestDB() as db:
        node_id = await _node(db)
        await db.execute(
            NodeStat.__table__.insert(),
            [_sample(node_id, 20), _sample(node_id, 15), _sample(node_id, 13), _sample(node_id, 1)],
        )
        await db.commit()

    await job.cleanup_node_stats()

    async with GetTestDB() as db:
        remaining = await db.scalar(select(func.count()).select_from(NodeStat))
    assert remaining == 2


@pytest.mark.asyncio
async def test_a_zero_window_keeps_everything(monkeypatch: pytest.MonkeyPatch):
    from app.fork.jobs import cleanup_node_stats as job

    monkeypatch.setattr(job, "GetDB", GetTestDB)
    monkeypatch.setattr(job.job_settings, "node_stats_retention_days", 0)

    async with GetTestDB() as db:
        node_id = await _node(db)
        await db.execute(NodeStat.__table__.insert(), [_sample(node_id, 400), _sample(node_id, 1)])
        await db.commit()

    await job.cleanup_node_stats()

    async with GetTestDB() as db:
        remaining = await db.scalar(select(func.count()).select_from(NodeStat))
    assert remaining == 2
