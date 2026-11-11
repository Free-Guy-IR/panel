import asyncio

import sqlalchemy

from app.db import base

_loops: list[asyncio.AbstractEventLoop] = []


async def _select_one() -> None:
    async with base.SessionLocal() as db:
        await db.execute(sqlalchemy.text("SELECT 1"))


async def test_a_pooled_connection_is_left_behind_for_the_next_async_test():
    _loops.append(asyncio.get_running_loop())
    await _select_one()


async def test_the_pooled_connection_still_belongs_to_the_running_loop():
    assert _loops, "the first test of this module did not run"
    assert asyncio.get_running_loop() is _loops[0]
    await _select_one()
