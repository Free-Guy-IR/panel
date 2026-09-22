from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.db.models import CoreConfig, CoreType, Node, NodeInboundUsage, NodeUsage
from tests.api import GetTestDB


@pytest.fixture(autouse=True)
async def _empty_usage_tables():
    async def clear():
        async with GetTestDB() as db:
            await db.execute(NodeUsage.__table__.delete())
            await db.execute(NodeInboundUsage.__table__.delete())
            await db.commit()

    await clear()
    yield
    await clear()


async def _node(db):
    node = (await db.scalars(select(Node).limit(1))).first()
    if node is not None:
        return node.id
    core = (await db.scalars(select(CoreConfig).limit(1))).first()
    if core is None:
        core = CoreConfig(name="usage-retention-core", config={}, type=CoreType.xray)
        db.add(core)
        await db.flush()
    node = Node(
        name="usage-retention-node",
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


def _node_usage(node_id, age_days):
    return {
        "node_id": node_id,
        "uplink": 1,
        "downlink": 1,
        "created_at": datetime.now(UTC) - timedelta(days=age_days),
    }


def _inbound_usage(node_id, age_days, tag):
    return {
        "node_id": node_id,
        "inbound_tag": tag,
        "uplink": 1,
        "downlink": 1,
        "created_at": datetime.now(UTC) - timedelta(days=age_days),
    }


async def _counts():
    async with GetTestDB() as db:
        return (
            await db.scalar(select(func.count()).select_from(NodeUsage)),
            await db.scalar(select(func.count()).select_from(NodeInboundUsage)),
        )


@pytest.mark.asyncio
async def test_both_usage_tables_are_pruned_past_the_window(monkeypatch: pytest.MonkeyPatch):
    from app.fork.jobs import cleanup_node_usages as job

    monkeypatch.setattr(job, "GetDB", GetTestDB)
    monkeypatch.setattr(job.job_settings, "node_usages_retention_days", 30)

    async with GetTestDB() as db:
        node_id = await _node(db)
        await db.execute(
            NodeUsage.__table__.insert(),
            [_node_usage(node_id, 40), _node_usage(node_id, 31), _node_usage(node_id, 29), _node_usage(node_id, 1)],
        )
        await db.execute(
            NodeInboundUsage.__table__.insert(),
            [
                _inbound_usage(node_id, 40, "vless-old"),
                _inbound_usage(node_id, 29, "vless-recent"),
                _inbound_usage(node_id, 1, "vless-today"),
            ],
        )
        await db.commit()

    await job.cleanup_node_usages()

    assert await _counts() == (2, 2)


@pytest.mark.asyncio
async def test_a_zero_window_keeps_everything(monkeypatch: pytest.MonkeyPatch):
    from app.fork.jobs import cleanup_node_usages as job

    monkeypatch.setattr(job, "GetDB", GetTestDB)
    monkeypatch.setattr(job.job_settings, "node_usages_retention_days", 0)

    async with GetTestDB() as db:
        node_id = await _node(db)
        await db.execute(NodeUsage.__table__.insert(), [_node_usage(node_id, 4000), _node_usage(node_id, 1)])
        await db.execute(
            NodeInboundUsage.__table__.insert(),
            [_inbound_usage(node_id, 4000, "vless-ancient"), _inbound_usage(node_id, 1, "vless-today")],
        )
        await db.commit()

    await job.cleanup_node_usages()

    assert await _counts() == (2, 2)


@pytest.mark.asyncio
async def test_a_backlog_larger_than_one_chunk_still_drains(monkeypatch: pytest.MonkeyPatch):
    from app.fork.jobs import cleanup_node_usages as job

    monkeypatch.setattr(job, "GetDB", GetTestDB)
    monkeypatch.setattr(job.job_settings, "node_usages_retention_days", 30)
    monkeypatch.setattr(job, "DELETE_CHUNK", 3)

    async with GetTestDB() as db:
        node_id = await _node(db)
        await db.execute(NodeUsage.__table__.insert(), [_node_usage(node_id, 40 + n) for n in range(10)])
        await db.execute(NodeUsage.__table__.insert(), [_node_usage(node_id, 1)])
        await db.commit()

    await job.cleanup_node_usages()

    node_usages, _ = await _counts()
    assert node_usages == 1


@pytest.mark.asyncio
async def test_one_run_never_deletes_more_than_its_ceiling(monkeypatch: pytest.MonkeyPatch):
    from app.fork.jobs import cleanup_node_usages as job

    monkeypatch.setattr(job, "GetDB", GetTestDB)
    monkeypatch.setattr(job.job_settings, "node_usages_retention_days", 30)
    monkeypatch.setattr(job, "DELETE_CHUNK", 2)
    monkeypatch.setattr(job, "MAX_PER_RUN", 4)

    async with GetTestDB() as db:
        node_id = await _node(db)
        await db.execute(NodeUsage.__table__.insert(), [_node_usage(node_id, 40 + n) for n in range(10)])
        await db.commit()

    await job.cleanup_node_usages()

    node_usages, _ = await _counts()
    assert node_usages == 6
