import asyncio
import contextlib

import pytest
from sqlalchemy import func, select

from app.db.models import UserSubscriptionUpdate
from app.subscription import sub_update_buffer
from tests.test_sub_update_buffer import buffer_db  # noqa: F401


@pytest.mark.asyncio
async def test_a_flush_cancelled_mid_write_keeps_every_row_for_the_final_flush(
    buffer_db,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
):
    session, user_id = buffer_db
    for index in range(3):
        await sub_update_buffer.queue_user_sub_update(user_id, f"client/{index}")
    await session.commit()

    real_get_db = sub_update_buffer.GetDB
    writing = asyncio.Event()

    class StuckGetDB(real_get_db):
        async def __aenter__(self):
            db = await super().__aenter__()

            async def stuck_scalars(*args, **kwargs):
                writing.set()
                await asyncio.Event().wait()

            db.scalars = stuck_scalars
            return db

    monkeypatch.setattr(sub_update_buffer, "GetDB", StuckGetDB)
    flush = asyncio.create_task(sub_update_buffer.flush_user_sub_updates())
    await asyncio.wait_for(writing.wait(), timeout=2.0)
    flush.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await flush

    assert sub_update_buffer.pending_count() == 3, "the batch taken for the cancelled write was dropped"

    monkeypatch.setattr(sub_update_buffer, "GetDB", real_get_db)
    assert await sub_update_buffer.flush_user_sub_updates() == 3
    stored = (await session.execute(select(func.count()).select_from(UserSubscriptionUpdate))).scalar()
    assert stored == 3
