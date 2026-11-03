from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.db.crud import user as user_crud
from app.db.models import Admin, User
from tests.test_admin_removal_during_owner_moves import INTO_ORDERS, _attempt
from tests.test_admin_wide_owner_moves_interleavings import (  # noqa: F401
    BOUND,
    STAGGER_SECONDS,
    _hold_after,
    _mover,
    quiet,
)
from tests.test_pv_r8_single_admin_removal_into import _single_remover
from tests.test_recorder_harness import _database_url, recorder_db_fixture, seed_users_and_nodes  # noqa: F401

pytestmark = pytest.mark.skipif(_database_url().startswith("sqlite"), reason="needs a server database")


@pytest.mark.asyncio
@pytest.mark.parametrize("order", INTO_ORDERS, ids=["-".join(order) for order in INTO_ORDERS])
async def test_single_admin_removal_never_detaches_users_moved_out_of_it(
    monkeypatch: pytest.MonkeyPatch, recorder_db, order
):
    home, _home_ids, _ = await seed_users_and_nodes(recorder_db, user_count=2, node_count=0)
    gone, gone_ids, _ = await seed_users_and_nodes(recorder_db, user_count=6, node_count=0)
    async with recorder_db() as session:
        home_name = (await session.execute(select(Admin.username).where(Admin.id == home))).scalar_one()

    _hold_after(monkeypatch, user_crud, "shift_admin_traffic")

    steps = {
        "mover": _mover(recorder_db, "mover", "bulk_set_owner", tuple(gone_ids), home_name),
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
    assert outcomes["mover"] == "committed", outcomes
    async with recorder_db() as session:
        owners = dict((await session.execute(select(User.id, User.admin_id).where(User.id.in_(gone_ids)))).all())
        remaining = (await session.execute(select(Admin.id).where(Admin.id == gone))).scalar_one_or_none()
    assert remaining is None, (remaining, outcomes)
    assert set(owners) == set(gone_ids), (owners, outcomes)
    assert set(owners.values()) == {home}, (owners, outcomes)
