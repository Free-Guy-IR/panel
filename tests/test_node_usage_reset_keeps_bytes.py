from __future__ import annotations

import pytest
from sqlalchemy import func, select, update

from app.db.models import Node
from tests.test_recorder_harness import (  # noqa: F401
    recorder_db_fixture,
    seed_users_and_nodes,
)


@pytest.mark.asyncio
async def test_node_reset_keeps_bytes_recorded_between_its_read_and_its_write(recorder_db):
    from app.db.crud.node import bulk_reset_node_usage
    from app.db.models import NodeUsageResetLogs

    _, _, node_ids = await seed_users_and_nodes(recorder_db, user_count=0, node_count=1)
    node_id = node_ids[0]
    async with recorder_db() as session:
        await session.execute(update(Node).where(Node.id == node_id).values(uplink=1_000, downlink=2_000))
        await session.commit()

    async with recorder_db() as db:
        nodes = list((await db.execute(select(Node).where(Node.id == node_id))).scalars().all())
        async with recorder_db() as recorder_session:
            await recorder_session.execute(
                update(Node).where(Node.id == node_id).values(uplink=Node.uplink + 300, downlink=Node.downlink + 400)
            )
            await recorder_session.commit()
        await bulk_reset_node_usage(db, nodes)

    async with recorder_db() as session:
        current = (await session.execute(select(Node.uplink, Node.downlink).where(Node.id == node_id))).one()
        logged = (
            await session.execute(
                select(func.sum(NodeUsageResetLogs.uplink), func.sum(NodeUsageResetLogs.downlink)).where(
                    NodeUsageResetLogs.node_id == node_id
                )
            )
        ).one()
    assert (current[0] + logged[0], current[1] + logged[1]) == (1_300, 2_400), (current, logged)


@pytest.mark.asyncio
async def test_single_node_reset_keeps_bytes_recorded_between_its_read_and_its_write(recorder_db):
    from app.db.crud.node import reset_node_usage
    from app.db.models import NodeUsageResetLogs

    _, _, node_ids = await seed_users_and_nodes(recorder_db, user_count=0, node_count=1)
    node_id = node_ids[0]
    async with recorder_db() as session:
        await session.execute(update(Node).where(Node.id == node_id).values(uplink=5_000, downlink=7_000))
        await session.commit()

    async with recorder_db() as db:
        db_node = (await db.execute(select(Node).where(Node.id == node_id))).scalar_one()
        async with recorder_db() as recorder_session:
            await recorder_session.execute(
                update(Node).where(Node.id == node_id).values(uplink=Node.uplink + 11, downlink=Node.downlink + 13)
            )
            await recorder_session.commit()
        await reset_node_usage(db, db_node)
        assert (db_node.uplink, db_node.downlink) == (0, 0)

    async with recorder_db() as session:
        current = (await session.execute(select(Node.uplink, Node.downlink).where(Node.id == node_id))).one()
        logged = (
            await session.execute(
                select(NodeUsageResetLogs.uplink, NodeUsageResetLogs.downlink).where(
                    NodeUsageResetLogs.node_id == node_id
                )
            )
        ).one()
    assert tuple(current) == (0, 0)
    assert tuple(logged) == (5_011, 7_013)


@pytest.mark.asyncio
async def test_bytes_landing_between_the_locked_read_and_the_reset_stay_on_the_counter(recorder_db):
    from sqlalchemy import event

    from app.db.crud.node import bulk_reset_node_usage
    from app.db.models import NodeUsageResetLogs
    from app.jobs import record_usages

    _, _, node_ids = await seed_users_and_nodes(recorder_db, user_count=0, node_count=1)
    node_id = node_ids[0]
    async with recorder_db() as session:
        await session.execute(update(Node).where(Node.id == node_id).values(uplink=1_000, downlink=2_000))
        await session.commit()

    engine = record_usages.engine.sync_engine
    landed = {"done": False}

    def increment_before_the_reset(conn, cursor, statement, parameters, context, executemany):
        normalized = " ".join(statement.upper().split())
        if not landed["done"] and normalized.startswith("UPDATE NODES SET UPLINK=(NODES.UPLINK -"):
            landed["done"] = True
            marker = "?" if conn.dialect.paramstyle == "qmark" else "%s"
            cursor.execute(
                f"UPDATE nodes SET uplink = uplink + 70, downlink = downlink + 90 WHERE id = {marker}", (node_id,)
            )

    event.listen(engine, "before_cursor_execute", increment_before_the_reset)
    try:
        async with recorder_db() as db:
            nodes = list((await db.execute(select(Node).where(Node.id == node_id))).scalars().all())
            await bulk_reset_node_usage(db, nodes)
    finally:
        event.remove(engine, "before_cursor_execute", increment_before_the_reset)

    async with recorder_db() as session:
        current = (await session.execute(select(Node.uplink, Node.downlink).where(Node.id == node_id))).one()
        logged = (
            await session.execute(
                select(func.sum(NodeUsageResetLogs.uplink), func.sum(NodeUsageResetLogs.downlink)).where(
                    NodeUsageResetLogs.node_id == node_id
                )
            )
        ).one()
    assert landed["done"]
    assert tuple(current) == (70, 90)
    assert (current[0] + logged[0], current[1] + logged[1]) == (1_070, 2_090)


@pytest.mark.asyncio
async def test_a_reset_only_moves_a_node_that_is_still_limited_back_to_connecting(recorder_db):
    from app.db.crud.node import bulk_reset_node_usage
    from app.db.models import NodeStatus

    _, _, node_ids = await seed_users_and_nodes(recorder_db, user_count=0, node_count=2)
    still_limited, disabled_meanwhile = node_ids
    async with recorder_db() as session:
        await session.execute(update(Node).where(Node.id.in_(node_ids)).values(status=NodeStatus.limited))
        await session.commit()

    async with recorder_db() as db:
        nodes = list((await db.execute(select(Node).where(Node.id.in_(node_ids)))).scalars().all())
        async with recorder_db() as admin_session:
            await admin_session.execute(
                update(Node).where(Node.id == disabled_meanwhile).values(status=NodeStatus.disabled)
            )
            await admin_session.commit()
        await bulk_reset_node_usage(db, nodes)

    async with recorder_db() as session:
        statuses = dict((await session.execute(select(Node.id, Node.status))).all())
    assert statuses == {still_limited: NodeStatus.connecting, disabled_meanwhile: NodeStatus.disabled}
