from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError

from app.db.models import Node, NodeUserUsage, User
from app.jobs import record_usages
from tests.test_recorder_harness import _database_url, recorder_db_fixture, seed_users_and_nodes  # noqa: F401
from tests.test_status_lock_order_interleavings import _deadlocks

pytestmark = pytest.mark.skipif(
    not _database_url().startswith("mysql"),
    reason="the node lock order inversion needs InnoDB foreign key share locks",
)

BOUND = 60
ROUNDS = 12


def _errno(err: BaseException) -> int | None:
    args = getattr(getattr(err, "orig", err), "args", None)
    return args[0] if args and isinstance(args[0], int) else None


async def _waiting_transactions(engine) -> int:
    async with engine.connect() as probe:
        return int(
            (
                await probe.execute(
                    text("SELECT COUNT(*) FROM information_schema.INNODB_TRX WHERE trx_state = 'LOCK WAIT'")
                )
            ).scalar_one()
        )


async def _until_a_transaction_waits(engine, writer: asyncio.Task) -> None:
    deadline = time.monotonic() + BOUND
    while time.monotonic() < deadline:
        if writer.done():
            raise AssertionError(f"the user usage writer finished without waiting for the node row: {writer.result()}")
        if await _waiting_transactions(engine):
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the user usage writer never waited for the node row")


def _count_attempts(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    attempts: list[int] = []
    real_settle = record_usages.settle_batches

    async def counting_settle(conn, stat_type, batches):
        attempts.append(int(stat_type))
        return await real_settle(conn, stat_type, batches)

    monkeypatch.setattr(record_usages, "settle_batches", counting_settle)
    return attempts


async def _user_cohort(user_to_node: dict[int, int], value: int):
    chart_rows = await record_usages.record_user_stats_batched(
        {node_id: [{"uid": user_id, "value": value}] for user_id, node_id in user_to_node.items()},
        {node_id: 1 for node_id in user_to_node.values()},
    )
    users_usage = [{"uid": user_id, "value": value} for user_id in sorted(user_to_node)]
    context = await record_usages.load_user_usage_context(set(user_to_node))
    return users_usage, context, {user_id: 0 for user_id in user_to_node}, chart_rows


@pytest.mark.asyncio
async def test_a_user_usage_cohort_and_a_node_usage_write_never_deadlock_on_the_node_rows(
    recorder_db, monkeypatch: pytest.MonkeyPatch
):
    _, (first_user, second_user), (low_node, high_node) = await seed_users_and_nodes(
        recorder_db, user_count=2, node_count=2
    )
    users_usage, context, epochs, chart_rows = await _user_cohort({first_user: high_node, second_user: low_node}, 100)
    assert [(row["uid"], row["node_id"]) for row in chart_rows] == [(first_user, high_node), (second_user, low_node)]
    attempts = _count_attempts(monkeypatch)
    engine = record_usages.engine
    deadlocks_before = await _deadlocks()

    async with engine.connect() as node_writer:
        await node_writer.execution_options(isolation_level="READ COMMITTED")
        await node_writer.begin()
        await node_writer.execute(update(Node).where(Node.id == low_node).values(uplink=Node.uplink + 1))
        user_writer = asyncio.create_task(
            record_usages.apply_fenced_user_usage(users_usage, context, epochs, chart_rows)
        )
        try:
            await _until_a_transaction_waits(engine, user_writer)
            try:
                await node_writer.execute(update(Node).where(Node.id == high_node).values(uplink=Node.uplink + 1))
                await node_writer.commit()
                node_outcome = "committed"
            except DBAPIError as err:
                node_outcome = f"MySQL {_errno(err)}"
                await node_writer.rollback()
            outcome = await asyncio.wait_for(user_writer, BOUND)
        finally:
            if not user_writer.done():
                user_writer.cancel()
                await asyncio.gather(user_writer, return_exceptions=True)

    deadlocks = await _deadlocks() - deadlocks_before
    assert (node_outcome, len(attempts), deadlocks) == ("committed", 1, 0)
    assert outcome.applied_users == 2
    async with recorder_db() as session:
        written = (
            await session.execute(select(NodeUserUsage.user_id, NodeUserUsage.node_id, NodeUserUsage.used_traffic))
        ).all()
        uplinks = dict((await session.execute(select(Node.id, Node.uplink))).all())
        traffic = dict((await session.execute(select(User.id, User.used_traffic))).all())
    assert sorted(written) == sorted([(first_user, high_node, 100), (second_user, low_node, 100)])
    assert uplinks == {low_node: 1, high_node: 1}
    assert traffic == {first_user: 100, second_user: 100}


@pytest.mark.asyncio
async def test_overlapping_user_and_node_usage_writers_with_inverted_node_orders_never_deadlock(
    recorder_db, monkeypatch: pytest.MonkeyPatch
):
    _, user_ids, node_ids = await seed_users_and_nodes(recorder_db, user_count=6, node_count=6)
    user_to_node = dict(zip(user_ids, reversed(node_ids), strict=True))
    attempts = _count_attempts(monkeypatch)
    deadlocks_before = await _deadlocks()

    for _ in range(ROUNDS):
        users_usage, context, epochs, chart_rows = await _user_cohort(user_to_node, 10)
        await asyncio.wait_for(
            asyncio.gather(
                record_usages.apply_fenced_user_usage(users_usage, context, epochs, chart_rows),
                record_usages.write_node_usage(
                    {node_id: (1, 1) for node_id in node_ids}, record_usages._get_time_bucket()
                ),
            ),
            BOUND,
        )

    deadlocks = await _deadlocks() - deadlocks_before
    user_attempts = attempts.count(int(record_usages.StatType.UsersStat))
    assert (deadlocks, user_attempts) == (0, ROUNDS)
    async with recorder_db() as session:
        uplinks = dict((await session.execute(select(Node.id, Node.uplink))).all())
        traffic = dict((await session.execute(select(User.id, User.used_traffic))).all())
    assert uplinks == {node_id: ROUNDS for node_id in node_ids}
    assert traffic == {user_id: 10 * ROUNDS for user_id in user_ids}
