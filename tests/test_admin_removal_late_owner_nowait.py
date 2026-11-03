from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select, update

from app.db.crud import user as user_crud
from app.db.models import Admin, User
from app.fork.crud import admin_scope
from tests.test_admin_removal_during_owner_moves import _attempt, _remover
from tests.test_admin_wide_owner_moves_interleavings import BOUND, _mover, quiet  # noqa: F401
from tests.test_recorder_harness import _database_url, recorder_db_fixture, seed_users_and_nodes  # noqa: F401
from tests.test_status_lock_order_interleavings import _deadlocks

pytestmark = pytest.mark.skipif(_database_url().startswith("sqlite"), reason="needs a server database")

SETTLE_SECONDS = 0.5


@pytest.mark.asyncio
@pytest.mark.parametrize("mover", ["set_owner", "bulk_set_owner"])
async def test_a_user_carried_onto_a_removed_admin_between_its_passes_and_moved_away_never_deadlocks(
    monkeypatch: pytest.MonkeyPatch, recorder_db, mover
):
    gone, gone_ids, _ = await seed_users_and_nodes(recorder_db, user_count=2, node_count=0)
    away, (late,), _ = await seed_users_and_nodes(recorder_db, user_count=1, node_count=0)
    target, target_ids, _ = await seed_users_and_nodes(recorder_db, user_count=1, node_count=0)
    everyone = [*gone_ids, late, *target_ids]
    async with recorder_db() as session:
        await session.execute(update(User).where(User.id.in_(everyone)).values(used_traffic=0))
        await session.execute(update(Admin).where(Admin.id.in_([gone, away, target])).values(used_traffic=0))
        await session.commit()
        names = dict(
            (await session.execute(select(Admin.id, Admin.username).where(Admin.id.in_([gone, target])))).all()
        )

    first_pass_done = asyncio.Event()
    mover_holds_user = asyncio.Event()
    remover_holds_admin = asyncio.Event()
    passes: list[int] = []
    real_detach = admin_scope._detach_committed_owners
    real_lock_owner_rows = user_crud.lock_owner_rows

    async def gated_detach(db, admin_ids, *args, **kwargs):
        passes.append(len(passes) + 1)
        if len(passes) == 2:
            remover_holds_admin.set()
        result = await real_detach(db, admin_ids, *args, **kwargs)
        if len(passes) == 1:
            first_pass_done.set()
            await asyncio.wait_for(mover_holds_user.wait(), BOUND)
        return result

    async def gated_lock_owner_rows(db, users):
        locked = await real_lock_owner_rows(db, users)
        if db.info.get("label") == "mover" and not mover_holds_user.is_set():
            mover_holds_user.set()
            await asyncio.wait_for(remover_holds_admin.wait(), BOUND)
            await asyncio.sleep(SETTLE_SECONDS)
        return locked

    monkeypatch.setattr(admin_scope, "_detach_committed_owners", gated_detach)
    monkeypatch.setattr(user_crud, "lock_owner_rows", gated_lock_owner_rows)

    outcomes: dict[str, str] = {}
    failures: dict[str, BaseException] = {}
    deadlocks_before = await _deadlocks()
    remover = asyncio.create_task(_attempt("remover", _remover(recorder_db, gone), outcomes, failures))
    await asyncio.wait_for(first_pass_done.wait(), BOUND)
    await _attempt("carrier", _mover(recorder_db, "carrier", "set_owner", (late,), names[gone]), outcomes, failures)
    mover_task = asyncio.create_task(
        _attempt("mover", _mover(recorder_db, "mover", mover, (late,), names[target]), outcomes, failures)
    )
    await asyncio.wait_for(asyncio.gather(remover, mover_task), timeout=BOUND)
    await asyncio.sleep(1)
    deadlocks_after = await _deadlocks()

    assert outcomes == {"remover": "committed", "carrier": "committed", "mover": "committed"}, outcomes
    if deadlocks_before is not None:
        assert deadlocks_after - deadlocks_before == 0, outcomes

    async with recorder_db() as session:
        owners = dict((await session.execute(select(User.id, User.admin_id).where(User.id.in_(everyone)))).all())
        remaining = (await session.execute(select(Admin.id).where(Admin.id == gone))).scalars().all()
        stranded = (await session.execute(select(User.id).where(User.admin_id == gone))).scalars().all()

    assert remaining == [], outcomes
    assert stranded == [], (stranded, outcomes)
    assert owners[late] == target, (owners, outcomes)
    assert all(owners[user_id] is None for user_id in gone_ids), (owners, outcomes)
    assert all(owners[user_id] == target for user_id in target_ids), (owners, outcomes)
