import asyncio

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.crud import bulk as bulk_crud
from app.db.models import User
from app.fork.proxy_secrets.locks import is_deadlock, is_row_lock_conflict
from app.models.user import BulkUser
from tests.test_connection_limiter_mysql_locks import MYSQL_URL, _fresh_schema

pytestmark = pytest.mark.skipif(not MYSQL_URL.startswith("mysql"), reason="TEST_MYSQL_URL is not set")


async def _wait_for_a_lock_wait(factory) -> None:
    for _ in range(200):
        async with factory() as probe:
            waiting = (
                await probe.execute(
                    text("SELECT COUNT(*) FROM information_schema.innodb_trx WHERE trx_state = 'LOCK WAIT'")
                )
            ).scalar_one()
        if waiting:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the ascending locker never waited on the bulk's row lock")


@pytest.mark.asyncio
async def test_the_late_rescan_does_not_wait_on_an_id_below_the_ids_the_bulk_holds(monkeypatch: pytest.MonkeyPatch):
    low, high = sorted(await _fresh_schema(2))
    engine = create_async_engine(MYSQL_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    locker = factory()
    locker_waits_for_high = None
    reads: list[set[int]] = []
    real = bulk_crud._committed_matching_ids

    async def ascending_locker_takes_low_then_waits_for_high(db, condition):
        nonlocal locker_waits_for_high
        if reads:
            async with factory() as writer:
                await writer.execute(update(User).where(User.id == low).values(data_limit=10**9))
                await writer.commit()
            await locker.execute(select(User.id).where(User.id == low).with_for_update())
            locker_waits_for_high = asyncio.create_task(
                locker.execute(select(User.id).where(User.id == high).with_for_update())
            )
            await _wait_for_a_lock_wait(factory)
        ids = await real(db, condition)
        reads.append(ids)
        return ids

    try:
        async with factory() as db:
            await db.execute(update(User).where(User.id == low).values(data_limit=None))
            await db.execute(update(User).where(User.id == high).values(data_limit=10**9))
            await db.commit()

        monkeypatch.setattr(bulk_crud, "_committed_matching_ids", ascending_locker_takes_low_then_waits_for_high)

        async with factory() as db:
            with pytest.raises(DBAPIError) as raised:
                await asyncio.wait_for(
                    bulk_crud.get_bulk_user_limit_targets(db, BulkUser(amount=1), "data_limit"), timeout=20
                )
            assert is_row_lock_conflict(raised.value, db.get_bind().dialect)
            assert not is_deadlock(raised.value)
            await db.rollback()

        await asyncio.wait_for(locker_waits_for_high, timeout=20)
        await locker.commit()
        assert reads == [{high}, {low, high}]

        monkeypatch.setattr(bulk_crud, "_committed_matching_ids", real)
        async with factory() as db:
            targets = await asyncio.wait_for(
                bulk_crud.get_bulk_user_limit_targets(db, BulkUser(amount=1), "data_limit"), timeout=20
            )
            await db.commit()
        assert sorted(row[0] for row in targets) == [low, high]
    finally:
        if locker_waits_for_high is not None and not locker_waits_for_high.done():
            locker_waits_for_high.cancel()
        await locker.close()
        await engine.dispose()
