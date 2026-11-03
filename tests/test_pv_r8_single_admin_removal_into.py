from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.db.crud import user as user_crud
from app.db.models import Admin, User
from app.operation import OperatorType
from app.operation.admin import AdminOperation
from tests.test_admin_removal_during_owner_moves import INTO_ORDERS, _attempt, _target_admin_was_deleted
from tests.test_admin_wide_owner_moves_interleavings import (  # noqa: F401
    BOUND,
    OWNER,
    STAGGER_SECONDS,
    _hold_after,
    _mover,
    quiet,
)
from tests.test_recorder_harness import _database_url, recorder_db_fixture, seed_users_and_nodes  # noqa: F401

pytestmark = pytest.mark.skipif(_database_url().startswith("sqlite"), reason="needs a server database")


def _single_remover(recorder_db, admin_id: int):
    async def run():
        async with recorder_db() as db:
            db.info["label"] = "remover"
            await AdminOperation(OperatorType.API).remove_admin_by_id(db, admin_id, OWNER)

    return run


@pytest.mark.asyncio
@pytest.mark.parametrize("order", INTO_ORDERS, ids=["-".join(order) for order in INTO_ORDERS])
@pytest.mark.parametrize("mover", ["set_owner", "bulk_set_owner"])
async def test_single_admin_removal_survives_users_moved_into_it(
    monkeypatch: pytest.MonkeyPatch, recorder_db, mover, order
):
    _away, away_ids, _ = await seed_users_and_nodes(recorder_db, user_count=30, node_count=0)
    gone, _gone_ids, _ = await seed_users_and_nodes(recorder_db, user_count=6, node_count=0)
    moved_ids = tuple(away_ids[:1]) if mover == "set_owner" else tuple(away_ids)
    async with recorder_db() as session:
        gone_name = (await session.execute(select(Admin.username).where(Admin.id == gone))).scalar_one()

    _hold_after(monkeypatch, user_crud, "shift_admin_traffic")

    steps = {
        "mover": _mover(recorder_db, "mover", mover, moved_ids, gone_name),
        "remover": _single_remover(recorder_db, gone),
    }
    outcomes: dict[str, str] = {}
    failures: dict[str, BaseException] = {}
    tasks = []
    for label in order:
        tasks.append(asyncio.create_task(_attempt(label, steps[label], outcomes, failures)))
        await asyncio.sleep(STAGGER_SECONDS)
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=BOUND)

    assert outcomes["remover"] == "committed", outcomes
    assert outcomes["mover"] == "committed" or _target_admin_was_deleted(failures["mover"]), outcomes
    async with recorder_db() as session:
        still_owned = (await session.execute(select(User.id).where(User.admin_id == gone))).scalars().all()
        remaining = (await session.execute(select(Admin.id).where(Admin.id == gone))).scalar_one_or_none()
    assert remaining is None, (remaining, outcomes)
    assert still_owned == [], (still_owned, outcomes)
