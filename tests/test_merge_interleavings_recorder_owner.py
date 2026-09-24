from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import joinedload

from app.db.crud.admin import reset_admin_usage
from app.db.crud.user import bulk_set_owner, set_owner
from app.db.models import Admin, AdminUsageLogs, User
from app.jobs import record_usages
from tests.test_recorder_harness import CountingNode, recorder_db_fixture, seed_users_and_nodes  # noqa: F401

BOUND = 30


async def _seed_two_owners(recorder_db, user_count: int, used: int, old_admin_used: int, new_admin_used: int):
    old_admin_id, user_ids, node_ids = await seed_users_and_nodes(recorder_db, user_count=user_count, node_count=1)
    new_admin_id, _, _ = await seed_users_and_nodes(recorder_db, user_count=0, node_count=0)
    async with recorder_db() as session:
        await session.execute(update(User).where(User.id.in_(user_ids)).values(used_traffic=used))
        await session.execute(update(Admin).where(Admin.id == old_admin_id).values(used_traffic=old_admin_used))
        await session.execute(update(Admin).where(Admin.id == new_admin_id).values(used_traffic=new_admin_used))
        await session.commit()
    return old_admin_id, new_admin_id, user_ids, node_ids[0]


def _node_billing(monkeypatch: pytest.MonkeyPatch, node_id: int, counters: dict[int, int]) -> None:
    node = CountingNode(node_id, counters)
    monkeypatch.setattr(record_usages.node_manager, "get_healthy_nodes", AsyncMock(return_value=[(node.node_id, node)]))


async def _tick() -> None:
    await asyncio.wait_for(record_usages._record_user_usages_impl(), BOUND)


async def _admins(recorder_db) -> dict[int, int]:
    async with recorder_db() as session:
        return {admin_id: int(used) for admin_id, used in (await session.execute(select(Admin.id, Admin.used_traffic)))}


async def _users(recorder_db, user_ids: list[int]) -> dict[int, tuple[int, int]]:
    async with recorder_db() as session:
        rows = await session.execute(select(User.id, User.admin_id, User.used_traffic).where(User.id.in_(user_ids)))
        return {user_id: (admin_id, int(used)) for user_id, admin_id, used in rows}


async def _load(session, user_ids: list[int], admin_id: int):
    users = list(
        (await session.execute(select(User).options(joinedload(User.admin)).where(User.id.in_(user_ids))))
        .unique()
        .scalars()
        .all()
    )
    admin = (await session.execute(select(Admin).where(Admin.id == admin_id))).scalar_one()
    return users, admin


@pytest.mark.asyncio
async def test_a_real_usage_tick_between_the_owner_change_read_and_its_move_moves_the_new_bytes_too(
    monkeypatch: pytest.MonkeyPatch, recorder_db
):
    old_id, new_id, (user_id,), node_id = await _seed_two_owners(recorder_db, 1, 1000, 5000, 700)
    _node_billing(monkeypatch, node_id, {user_id: 300})

    async with recorder_db() as owner_change:
        (db_user,), new_admin = await _load(owner_change, [user_id], new_id)
        assert db_user.used_traffic == 1000
        await _tick()
        await asyncio.wait_for(set_owner(owner_change, db_user, new_admin), BOUND)

    assert await _users(recorder_db, [user_id]) == {user_id: (new_id, 1300)}
    admins = await _admins(recorder_db)
    assert (admins[old_id], admins[new_id]) == (5000 + 300 - 1300, 700 + 1300), admins


@pytest.mark.asyncio
async def test_a_bulk_owner_change_around_a_tick_billing_some_of_its_users_conserves_every_byte(
    monkeypatch: pytest.MonkeyPatch, recorder_db
):
    old_id, new_id, user_ids, node_id = await _seed_two_owners(recorder_db, 3, 100, 900, 0)
    billed = {user_ids[0]: 40, user_ids[2]: 60}
    _node_billing(monkeypatch, node_id, billed)

    async with recorder_db() as owner_change:
        users, new_admin = await _load(owner_change, user_ids, new_id)
        await _tick()
        await asyncio.wait_for(bulk_set_owner(owner_change, users, new_admin), BOUND)

    rows = await _users(recorder_db, user_ids)
    assert rows == {user_id: (new_id, 100 + billed.get(user_id, 0)) for user_id in user_ids}
    admins = await _admins(recorder_db)
    assert admins[new_id] == sum(used for _admin, used in rows.values())
    assert admins[old_id] == 900 + sum(billed.values()) - admins[new_id]


@pytest.mark.asyncio
async def test_an_owner_change_that_read_before_a_tick_and_a_reset_of_the_old_owner_loses_nothing(
    monkeypatch: pytest.MonkeyPatch, recorder_db
):
    old_id, new_id, (user_id,), node_id = await _seed_two_owners(recorder_db, 1, 1000, 1000, 0)
    _node_billing(monkeypatch, node_id, {user_id: 250})

    async with recorder_db() as owner_change:
        (db_user,), new_admin = await _load(owner_change, [user_id], new_id)
        async with recorder_db() as reset_session:
            old_admin = (await reset_session.execute(select(Admin).where(Admin.id == old_id))).scalar_one()
            await _tick()
            await asyncio.wait_for(reset_admin_usage(reset_session, old_admin), BOUND)
        await asyncio.wait_for(set_owner(owner_change, db_user, new_admin), BOUND)

    assert await _users(recorder_db, [user_id]) == {user_id: (new_id, 1250)}
    async with recorder_db() as session:
        logged = (
            await session.execute(select(AdminUsageLogs.used_traffic_at_reset).where(AdminUsageLogs.admin_id == old_id))
        ).scalar_one()
    admins = await _admins(recorder_db)
    assert logged == 1000
    assert (admins[old_id], admins[new_id]) == (1000 + 250 - logged - 1250, 1250), admins
