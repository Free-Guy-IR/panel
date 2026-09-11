from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db import base
from app.db.crud.node import remove_node, remove_nodes
from app.db.models import Node, NodeInboundUsage, NodeStat, NodeUsage, NodeUsageResetLogs, NodeUserUsage


@pytest.fixture
async def db_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(base.Base.metadata.create_all)

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as session:
        yield session

    await engine.dispose()


def _node(name, port):
    return Node(
        name=name,
        address="10.0.0.1",
        port=port,
        api_port=port + 1,
        server_ca="ca",
        api_key="key",
        core_config_id=None,
    )


def _usage_rows(node_id, bucket):
    return [
        NodeUserUsage(created_at=bucket, user_id=1, node_id=node_id, used_traffic=10),
        NodeUsage(created_at=bucket, node_id=node_id, uplink=1, downlink=2),
        NodeInboundUsage(created_at=bucket, node_id=node_id, inbound_tag="vless", uplink=3, downlink=4),
        NodeUsageResetLogs(node_id=node_id, uplink=5, downlink=6),
    ]


@pytest.mark.asyncio
async def test_remove_node_detaches_usage_history_instead_of_deleting_it(db_session):
    node = _node("doomed", 2000)
    db_session.add(node)
    await db_session.commit()
    bucket = datetime(2026, 9, 1, tzinfo=UTC)
    db_session.add_all(_usage_rows(node.id, bucket))
    db_session.add(
        NodeStat(
            node_id=node.id,
            mem_total=1,
            mem_used=1,
            cpu_cores=1,
            cpu_usage=1,
            incoming_bandwidth_speed=1,
            outgoing_bandwidth_speed=1,
        )
    )
    await db_session.commit()

    await remove_node(db_session, node)

    assert await db_session.get(Node, node.id) is None
    assert (await db_session.execute(select(func.count()).select_from(NodeStat))).scalar() == 0
    for table in (NodeUserUsage, NodeUsage, NodeInboundUsage, NodeUsageResetLogs):
        rows = (await db_session.execute(select(table.node_id))).scalars().all()
        assert rows == [None]


@pytest.mark.asyncio
async def test_remove_nodes_detaches_history_for_every_removed_node(db_session):
    nodes = [_node("doomed-1", 2002), _node("doomed-2", 2004), _node("kept", 2006)]
    db_session.add_all(nodes)
    await db_session.commit()
    bucket = datetime(2026, 9, 1, tzinfo=UTC)
    for node in nodes:
        db_session.add_all(_usage_rows(node.id, bucket))
    await db_session.commit()

    await remove_nodes(db_session, [nodes[0].id, nodes[1].id])

    remaining = (await db_session.execute(select(Node.name))).scalars().all()
    assert remaining == ["kept"]
    for table in (NodeUserUsage, NodeUsage, NodeInboundUsage, NodeUsageResetLogs):
        rows = sorted(
            (await db_session.execute(select(table.node_id))).scalars().all(),
            key=lambda v: (v is not None, v or 0),
        )
        assert rows == [None, None, nodes[2].id]
