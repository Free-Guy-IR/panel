from __future__ import annotations

import asyncio
from contextlib import contextmanager

import pytest
from sqlalchemy import event, func, select, update

from app.db.models import User
from app.jobs import record_usages
from tests.test_recorder_harness import (  # noqa: F401
    _database_url,
    is_billing_write,
    recorder_db_fixture,
    seed_users_and_nodes,
)

server_only = pytest.mark.skipif(
    _database_url().startswith("sqlite"), reason="row locks and the unnest UPDATE need a server database"
)


async def _cohort(user_ids: list[int], values: list[int]):
    usage = [{"uid": uid, "value": value} for uid, value in zip(user_ids, values)]
    context = await record_usages.load_user_usage_context(set(user_ids))
    epochs = await record_usages.current_usage_epochs()
    return usage, context, epochs


async def _rows(session_factory, user_ids: list[int]) -> dict[int, tuple[int, int]]:
    async with session_factory() as session:
        result = await session.execute(
            select(User.id, User.used_traffic, User.usage_epoch).where(User.id.in_(user_ids))
        )
        return {int(uid): (int(used), int(epoch)) for uid, used, epoch in result.all()}


async def _reset(session_factory, user_id: int) -> None:
    async with session_factory() as session:
        await session.execute(
            update(User)
            .where(User.id == user_id)
            .values(used_traffic=0, usage_epoch=User.usage_epoch + 1)
            .execution_options(synchronize_session=False)
        )
        await session.commit()


@contextmanager
def _billing_writes():
    writes: list[tuple[str, bool]] = []

    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        if is_billing_write(statement):
            writes.append((statement, executemany))

    sync_engine = record_usages.engine.sync_engine
    event.listen(sync_engine, "before_cursor_execute", before_cursor_execute)
    try:
        yield writes
    finally:
        event.remove(sync_engine, "before_cursor_execute", before_cursor_execute)


@server_only
@pytest.mark.asyncio
async def test_the_postgresql_billing_write_is_one_unnest_update_fenced_on_the_usage_epoch(
    monkeypatch: pytest.MonkeyPatch, recorder_db
):
    if not _database_url().startswith("postgresql"):
        pytest.skip("the unnest UPDATE is the PostgreSQL billing write")
    _, user_ids, _ = await seed_users_and_nodes(recorder_db, user_count=3, node_count=1)
    usage, context, epochs = await _cohort(user_ids, [300, 70, 5])
    with _billing_writes() as writes:
        outcome = await record_usages.apply_fenced_user_usage(usage, context, epochs)

    assert len(writes) == 1
    statement, executemany = writes[0]
    assert not executemany
    assert "unnest" in statement.lower()
    assert "usage_epoch" in statement.lower()
    assert sorted(outcome.billed_user_ids) == sorted(user_ids)
    rows = await _rows(recorder_db, user_ids)
    assert {uid: used for uid, (used, _) in rows.items()} == dict(zip(user_ids, [300, 70, 5]))


@pytest.mark.asyncio
async def test_a_usage_epoch_that_moves_inside_the_billing_transaction_bills_nothing_to_that_user(
    monkeypatch: pytest.MonkeyPatch, recorder_db
):
    _, user_ids, _ = await seed_users_and_nodes(recorder_db, user_count=3, node_count=1)
    moved_id = user_ids[1]
    usage, context, epochs = await _cohort(user_ids, [300, 70, 5])
    real_updates = record_usages.fenced_user_traffic_updates

    def move_epoch_then_update(family, billable, batch_size):
        yield (
            update(User)
            .where(User.id == moved_id)
            .values(usage_epoch=User.usage_epoch + 1)
            .execution_options(synchronize_session=False),
            {},
        )
        yield from real_updates(family, billable, batch_size)

    monkeypatch.setattr(record_usages, "fenced_user_traffic_updates", move_epoch_then_update)

    await record_usages.apply_fenced_user_usage(usage, context, epochs)

    rows = await _rows(recorder_db, user_ids)
    assert rows[moved_id] == (0, epochs.get(moved_id, 0) + 1)
    assert rows[user_ids[0]][0] == 300
    assert rows[user_ids[2]][0] == 5


@server_only
@pytest.mark.asyncio
async def test_a_reset_committed_before_the_billing_lock_fences_the_user(monkeypatch: pytest.MonkeyPatch, recorder_db):
    _, user_ids, _ = await seed_users_and_nodes(recorder_db, user_count=2, node_count=1)
    reset_id, kept_id = user_ids
    usage, context, epochs = await _cohort(user_ids, [900, 70])
    real_settle = record_usages.settle_batches
    fired = {"count": 0}

    async def settle_after_concurrent_reset(conn, stat_type, stat_batches):
        if not fired["count"]:
            fired["count"] += 1
            await asyncio.wait_for(_reset(recorder_db, reset_id), timeout=10)
        return await real_settle(conn, stat_type, stat_batches)

    monkeypatch.setattr(record_usages, "settle_batches", settle_after_concurrent_reset)

    outcome = await record_usages.apply_fenced_user_usage(usage, context, epochs)

    assert fired["count"] == 1
    assert reset_id in outcome.fenced_user_ids
    rows = await _rows(recorder_db, user_ids)
    assert rows[reset_id] == (0, epochs.get(reset_id, 0) + 1)
    assert rows[kept_id][0] == 70


@server_only
@pytest.mark.asyncio
async def test_a_reset_racing_the_locked_billing_write_waits_for_it_and_leaves_zero(
    monkeypatch: pytest.MonkeyPatch, recorder_db
):
    _, user_ids, _ = await seed_users_and_nodes(recorder_db, user_count=2, node_count=1)
    reset_id, kept_id = user_ids
    usage, context, epochs = await _cohort(user_ids, [900, 70])
    real_updates = record_usages.fenced_user_traffic_updates
    racing: dict[str, asyncio.Task] = {}
    blocked = {"while_billing": None}
    sleep = func.pg_sleep if record_usages.sql_family(await record_usages.get_dialect()) == "postgresql" else func.sleep

    def update_while_a_reset_waits(family, billable, batch_size):
        if "reset" not in racing:
            racing["reset"] = asyncio.create_task(_reset(recorder_db, reset_id))
            yield select(sleep(0.5)), {}
            blocked["while_billing"] = not racing["reset"].done()
        yield from real_updates(family, billable, batch_size)

    monkeypatch.setattr(record_usages, "fenced_user_traffic_updates", update_while_a_reset_waits)

    outcome = await record_usages.apply_fenced_user_usage(usage, context, epochs)
    await asyncio.wait_for(racing["reset"], timeout=10)

    assert blocked["while_billing"] is True
    assert reset_id in outcome.billed_user_ids
    rows = await _rows(recorder_db, user_ids)
    assert rows[reset_id] == (0, epochs.get(reset_id, 0) + 1)
    assert rows[kept_id][0] == 70


@server_only
@pytest.mark.asyncio
async def test_resets_interleaved_with_repeated_billing_never_keep_pre_reset_bytes(
    monkeypatch: pytest.MonkeyPatch, recorder_db
):
    _, user_ids, _ = await seed_users_and_nodes(recorder_db, user_count=4, node_count=1)
    target = user_ids[0]

    for delay in (0.0, 0.001, 0.005, 0.01, 0.02, 0.05):
        usage, context, epochs = await _cohort(user_ids, [1000, 10, 20, 30])
        reset = asyncio.create_task(_reset_after(recorder_db, target, delay))
        outcome = await record_usages.apply_fenced_user_usage(usage, context, epochs)
        await asyncio.wait_for(reset, timeout=10)
        used, epoch = (await _rows(recorder_db, [target]))[target]
        assert epoch == epochs.get(target, 0) + 1
        assert used == 0, (delay, outcome)


async def _reset_after(session_factory, user_id: int, delay: float) -> None:
    await asyncio.sleep(delay)
    await _reset(session_factory, user_id)
