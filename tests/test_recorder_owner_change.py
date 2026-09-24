from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import joinedload

from app.db.crud.user import set_owner
from app.db.models import Admin, User
from app.jobs import record_usages
from tests.test_recorder_harness import (  # noqa: F401
    CountingNode,
    recorder_db_fixture,
    seed_users_and_nodes,
    usage_snapshot,
)


@pytest.mark.asyncio
async def test_bytes_billed_while_the_owner_changes_are_charged_to_the_new_owner(
    monkeypatch: pytest.MonkeyPatch, recorder_db
):
    old_admin_id, user_ids, node_ids = await seed_users_and_nodes(recorder_db, user_count=1, node_count=1)
    new_admin_id, _, _ = await seed_users_and_nodes(recorder_db, user_count=0, node_count=0)
    user_id = user_ids[0]
    async with recorder_db() as session:
        await session.execute(update(User).where(User.id == user_id).values(used_traffic=1000))
        await session.execute(update(Admin).where(Admin.id == old_admin_id).values(used_traffic=1000))
        await session.commit()

    node = CountingNode(node_ids[0], {user_id: 500})
    monkeypatch.setattr(record_usages.node_manager, "get_healthy_nodes", AsyncMock(return_value=[(node.node_id, node)]))

    real_context = record_usages.load_user_usage_context

    async def context_then_owner_changes(uids):
        context = await real_context(uids)
        async with recorder_db() as session:
            db_user = (
                await session.execute(select(User).options(joinedload(User.admin)).where(User.id == user_id))
            ).scalar_one()
            new_admin = (await session.execute(select(Admin).where(Admin.id == new_admin_id))).scalar_one()
            await set_owner(session, db_user, new_admin)
        return context

    monkeypatch.setattr(record_usages, "load_user_usage_context", context_then_owner_changes)

    await record_usages._record_user_usages_impl()

    async with recorder_db() as session:
        admins = dict((await session.execute(select(Admin.id, Admin.used_traffic))).all())
        user_row = (await session.execute(select(User.admin_id, User.used_traffic).where(User.id == user_id))).one()
    assert user_row == (new_admin_id, 1500)
    assert (admins[old_admin_id], admins[new_admin_id]) == (0, 1500), admins


@pytest.mark.asyncio
async def test_bytes_billed_while_the_user_loses_its_owner_credit_no_admin(
    monkeypatch: pytest.MonkeyPatch, recorder_db
):
    owner_id, user_ids, node_ids = await seed_users_and_nodes(recorder_db, user_count=2, node_count=1)
    orphaned, kept = user_ids
    node = CountingNode(node_ids[0], {orphaned: 400, kept: 250})
    monkeypatch.setattr(record_usages.node_manager, "get_healthy_nodes", AsyncMock(return_value=[(node.node_id, node)]))

    real_context = record_usages.load_user_usage_context

    async def context_then_owner_removed(uids):
        context = await real_context(uids)
        async with recorder_db() as session:
            await session.execute(update(User).where(User.id == orphaned).values(admin_id=None))
            await session.commit()
        return context

    monkeypatch.setattr(record_usages, "load_user_usage_context", context_then_owner_removed)

    await record_usages._record_user_usages_impl()

    billed, charted = await usage_snapshot(recorder_db, user_ids)
    async with recorder_db() as session:
        credited = (await session.execute(select(Admin.used_traffic).where(Admin.id == owner_id))).scalar_one()
    assert billed == {orphaned: 400, kept: 250}
    assert charted == billed
    assert credited == 250
