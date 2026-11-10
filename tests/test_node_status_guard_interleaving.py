from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select, update

from app.db.crud.node import update_node_status
from app.db.models import Node, NodeStatus
from app.operation.node import NodeOperation
from tests.test_recorder_harness import recorder_db_fixture, seed_users_and_nodes  # noqa: F401

WRITERS = 4


async def _seed_node(recorder_db, status: NodeStatus) -> int:
    _, _, node_ids = await seed_users_and_nodes(recorder_db, user_count=0, node_count=1)
    async with recorder_db() as session:
        await session.execute(update(Node).where(Node.id == node_ids[0]).values(status=status, message=""))
        await session.commit()
    return node_ids[0]


async def _row(recorder_db, node_id: int) -> tuple[NodeStatus, str]:
    async with recorder_db() as session:
        status, message = (await session.execute(select(Node.status, Node.message).where(Node.id == node_id))).one()
        return status, message


async def _observe(recorder_db, node_id: int) -> Node:
    async with recorder_db() as session:
        return (await session.execute(select(Node).where(Node.id == node_id))).scalar_one()


async def _write(recorder_db, node_id: int, status: NodeStatus, message: str, expected: NodeStatus | None):
    async with recorder_db() as session:
        db_node = (await session.execute(select(Node).where(Node.id == node_id))).scalar_one()
        return await update_node_status(session, db_node, status, message=message, expected_status=expected)


@pytest.mark.asyncio
async def test_a_probe_that_read_before_a_node_push_cannot_overwrite_the_newer_status(recorder_db):
    node_id = await _seed_node(recorder_db, NodeStatus.connected)

    observed = await _observe(recorder_db, node_id)
    assert observed.status is NodeStatus.connected

    pushed = await _write(recorder_db, node_id, NodeStatus.connecting, "reconnecting", None)
    assert pushed is not None

    async with recorder_db() as probe_session:
        superseded = await update_node_status(
            probe_session,
            observed,
            NodeStatus.error,
            message="probe failed",
            expected_status=NodeStatus.connected,
        )

    assert superseded is None
    assert await _row(recorder_db, node_id) == (NodeStatus.connecting, "reconnecting")

    unguarded = await _write(recorder_db, node_id, NodeStatus.error, "probe failed", None)
    assert unguarded is not None
    assert await _row(recorder_db, node_id) == (NodeStatus.error, "probe failed")


@pytest.mark.asyncio
async def test_overlapping_guarded_writers_move_the_node_once_and_lose_no_newer_update(recorder_db):
    node_id = await _seed_node(recorder_db, NodeStatus.connected)
    start = asyncio.Event()
    seen: list[NodeStatus] = []

    async def writer(index: int):
        observed = await _observe(recorder_db, node_id)
        assert observed.status is NodeStatus.connected
        await start.wait()
        async with recorder_db() as session:
            # A read interleaved with the other writers' commits, in this writer's session.
            seen.append((await session.execute(select(Node.status).where(Node.id == node_id))).scalar_one())
            return await update_node_status(
                session,
                observed,
                NodeStatus.error,
                message=f"writer-{index}",
                expected_status=NodeStatus.connected,
            )

    tasks = [asyncio.create_task(writer(index)) for index in range(WRITERS)]
    await asyncio.sleep(0)
    start.set()
    results = await asyncio.gather(*tasks)

    # Every writer observed the same connected node, so all but the one that moves it
    # are superseded. Without the guard each of them overwrites the one before it.
    accepted = [result for result in results if result is not None]
    assert 0 < len(accepted) < WRITERS, results
    status, message = await _row(recorder_db, node_id)
    assert status is NodeStatus.error
    assert message in {f"writer-{index}" for index in range(WRITERS)}
    # A reader never sees the node move back, and never sees a status nobody wrote.
    assert set(seen) <= {NodeStatus.connected, NodeStatus.error}, seen
    assert seen == sorted(seen, key=(NodeStatus.connected, NodeStatus.error).index), seen


@pytest.mark.asyncio
async def test_a_replayed_error_observation_does_not_undo_a_recovery(recorder_db):
    node_id = await _seed_node(recorder_db, NodeStatus.error)

    observed = await _observe(recorder_db, node_id)
    recovered = await _write(recorder_db, node_id, NodeStatus.connected, "", NodeStatus.error)
    assert recovered is not None

    async with recorder_db() as replay_session:
        replayed = await update_node_status(
            replay_session,
            observed,
            NodeStatus.error,
            message="stale backend health check failed",
            expected_status=NodeStatus.error,
        )

    assert replayed is None
    status, message = await _row(recorder_db, node_id)
    assert status is NodeStatus.connected
    assert message == ""


@pytest.mark.asyncio
async def test_the_operation_refuses_a_guarded_write_whose_observation_is_already_superseded(recorder_db):
    node_id = await _seed_node(recorder_db, NodeStatus.connected)

    async with recorder_db() as session:
        moved = await NodeOperation._update_single_node_status(
            session,
            node_id,
            NodeStatus.connecting,
            message="admin reconnect",
            send_notification=False,
        )
    assert moved is True

    async with recorder_db() as session:
        superseded = await NodeOperation._update_single_node_status(
            session,
            node_id,
            NodeStatus.error,
            message="probe failed",
            send_notification=False,
            expected_status=NodeStatus.connected,
        )
    assert superseded is False
    assert await _row(recorder_db, node_id) == (NodeStatus.connecting, "admin reconnect")

    async with recorder_db() as session:
        await session.execute(update(Node).where(Node.id == node_id).values(status=NodeStatus.disabled))
        await session.commit()

    async with recorder_db() as session:
        refused = await NodeOperation._update_single_node_status(
            session,
            node_id,
            NodeStatus.error,
            message="probe failed after disable",
            send_notification=False,
            expected_status=NodeStatus.disabled,
        )
    assert refused is False
    status, _ = await _row(recorder_db, node_id)
    assert status is NodeStatus.disabled
