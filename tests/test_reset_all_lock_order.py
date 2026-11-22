from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from app.jobs import record_usages
from config import database_settings
from tests.test_usage_rollup_interleavings import (  # noqa: F401
    WAITERS,
    _failures,
    _partner,
    _rollup,
    _seed,
    world_fixture,
)

pytestmark = pytest.mark.skipif(
    not database_settings.url.startswith("postgresql"), reason="builds a heap order that differs from the id order"
)
ORDERS = ["scan_differs_from_id", "scan_equals_id"]


async def _ids_in_heap_order(world) -> list[int]:
    async with world.engine.connect() as conn:
        return list((await conn.execute(text("select id from users order by ctid"))).scalars())


async def _lock_waiters(world) -> int:
    async with world.engine.connect() as conn:
        return (await conn.execute(text(WAITERS["postgresql"]))).scalar_one()


async def _until_waiters(world, count: int, *tasks: asyncio.Task) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 30
    while loop.time() < deadline:
        finished = [task for task in tasks if task.done()]
        if finished:
            raise AssertionError(
                f"finished before it waited: {[task.exception() or task.result() for task in finished]}"
            )
        if await _lock_waiters(world) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"never saw {count} lock waiters")


async def _rewrite(world, user_id: int) -> None:
    async with world.engine.begin() as conn:
        await conn.execute(text("update users set used_traffic = used_traffic where id = :id"), {"id": user_id})


async def _three_users_in(world, order: str) -> list[int]:
    user_ids, _ = await _seed(world, users=3)
    first, second, third = sorted(user_ids)
    if order == "scan_differs_from_id":
        await _rewrite(world, first)
        await _rewrite(world, second)
    expected = [third, first, second] if order == "scan_differs_from_id" else [first, second, third]
    assert await _ids_in_heap_order(world) == expected
    return [first, second, third]


async def _race_reset_all_against(world, locker, second: int, first: int) -> list:
    gate = await world.engine.connect()
    gate_tx = await gate.begin()
    await gate.execute(text("select id from users where id = :id for update"), {"id": second})
    holder = asyncio.create_task(locker())
    reset = None
    try:
        await _until_waiters(world, 1, holder)
        reset = asyncio.create_task(_partner(world, "reset_all", first))
        await _until_waiters(world, 2, holder, reset)
    finally:
        await gate_tx.rollback()
        await gate.close()
    tasks = [holder] + ([reset] if reset is not None else [])
    return await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 60)


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ORDERS)
async def test_reset_all_and_a_rollup_batch_lock_users_without_a_deadlock(world, order):
    first, second, _ = await _three_users_in(world, order)

    results = await _race_reset_all_against(world, lambda: _rollup(world), second, first)

    assert not _failures(results), [repr(result)[:200] for result in _failures(results)]


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ORDERS)
async def test_reset_all_and_a_usage_write_lock_users_without_a_deadlock(world, order):
    user_ids = await _three_users_in(world, order)
    first, second, _ = user_ids
    async with world.engine.connect() as conn:
        admins = dict((await conn.execute(text("select id, admin_id from users"))).all())
    usage = [{"uid": user_id, "value": 10} for user_id in user_ids]
    context = {user_id: record_usages.UserUsageContext(admin_id=admins[user_id], usage_epoch=0) for user_id in user_ids}
    epochs = {user_id: 0 for user_id in user_ids}

    results = await _race_reset_all_against(
        world, lambda: record_usages.apply_fenced_user_usage(usage, context, epochs), second, first
    )

    assert not _failures(results), [repr(result)[:200] for result in _failures(results)]
