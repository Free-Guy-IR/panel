from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select, update

from app.db.crud import user as user_crud
from app.db.crud.user import bulk_set_owner
from app.db.crud.user_template import remove_user_templates
from app.db.models import Admin, Group, NextPlan, User, UserStatus, UserTemplate
from app.fork.jobs import usage_limits
from app.jobs import record_usages
from tests.test_merge_interleavings_recorder_owner import BOUND, _admins, _load, _node_billing, _tick, _users
from tests.test_next_plan_vanished_in_review_jobs import events_fixture  # noqa: F401
from tests.test_recorder_harness import recorder_db_fixture, seed_users_and_nodes  # noqa: F401

GB = 1024**3


@pytest.mark.asyncio
async def test_a_bulk_owner_change_of_users_the_target_already_owns_around_a_tick_credits_only_moved_traffic(
    monkeypatch: pytest.MonkeyPatch, recorder_db
):
    old_id, moved, node_ids = await seed_users_and_nodes(recorder_db, user_count=2, node_count=1)
    new_id, owned, _ = await seed_users_and_nodes(recorder_db, user_count=2, node_count=0)
    user_ids = [*moved, *owned]
    async with recorder_db() as session:
        await session.execute(update(User).where(User.id.in_(user_ids)).values(used_traffic=100))
        await session.execute(update(Admin).where(Admin.id == old_id).values(used_traffic=900))
        await session.execute(update(Admin).where(Admin.id == new_id).values(used_traffic=500))
        await session.commit()
    billed = {moved[0]: 40, owned[0]: 60}
    _node_billing(monkeypatch, node_ids[0], billed)

    async with recorder_db() as owner_change:
        users, new_admin = await _load(owner_change, user_ids, new_id)
        await _tick()
        await asyncio.wait_for(bulk_set_owner(owner_change, users, new_admin), BOUND)

    rows = await _users(recorder_db, user_ids)
    assert rows == {user_id: (new_id, 100 + billed.get(user_id, 0)) for user_id in user_ids}, rows
    moved_traffic = sum(rows[user_id][1] for user_id in moved)
    admins = await _admins(recorder_db)
    assert admins[new_id] == 500 + billed[owned[0]] + moved_traffic, admins
    assert admins[old_id] == 900 + billed[moved[0]] - moved_traffic, admins


@pytest.mark.asyncio
async def test_a_bulk_template_delete_under_the_limit_job_limits_the_user_instead_of_renewing_it_unlimited(
    monkeypatch: pytest.MonkeyPatch, recorder_db, events
):
    monkeypatch.setattr(usage_limits, "GetDB", record_usages.GetDB)
    _, (user_id,), _ = await seed_users_and_nodes(recorder_db, user_count=1, node_count=0)
    async with recorder_db() as session:
        group = Group(name="lane-b-template-delete", inbounds=[])
        session.add(group)
        await session.flush()
        template = UserTemplate(
            name="lane-b-template-delete",
            username_prefix=None,
            username_suffix=None,
            extra_settings=None,
            groups=[group],
            data_limit=5 * GB,
            expire_duration=86400,
        )
        session.add(template)
        await session.flush()
        template_id = template.id
        await session.execute(
            update(User)
            .where(User.id == user_id)
            .values(status=UserStatus.active, data_limit=1000, used_traffic=1100, expire=None)
        )
        session.add(NextPlan(user_id=user_id, user_template_id=template_id))
        await session.commit()

    real_refresh = user_crud.refresh_user_state_for_update
    deleted = []

    async def refresh_then_the_template_is_bulk_deleted(db, db_user, *args, **kwargs):
        found = await real_refresh(db, db_user, *args, **kwargs)
        if db_user.id == user_id and not deleted:
            async with recorder_db() as admin_session:
                await remove_user_templates(admin_session, [template_id])
            deleted.append(template_id)
        return found

    monkeypatch.setattr(user_crud, "refresh_user_state_for_update", refresh_then_the_template_is_bulk_deleted)

    await usage_limits.enforce_user_limits_now([user_id])

    async with recorder_db() as session:
        row = (
            await session.execute(
                select(User.status, User.data_limit, User.used_traffic, User.expire).where(User.id == user_id)
            )
        ).one()
        plans = (await session.execute(select(NextPlan.id).where(NextPlan.user_id == user_id))).scalars().all()
    assert deleted == [template_id]
    assert (row.status, row.data_limit, row.used_traffic, row.expire) == (UserStatus.limited, 1000, 1100, None), row
    assert plans == [], plans
    assert events["next_plan"] == [], events
    assert events["status"] == [(user_id, UserStatus.limited)], events
