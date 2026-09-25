from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select, update

from app.db.crud import user as user_crud
from app.db.crud.user_template import remove_user_templates
from app.db.models import Group, NextPlan, User, UserStatus, UserTemplate
from app.fork.jobs import status_review
from app.jobs import record_usages, review_users
from tests.test_recorder_harness import recorder_db_fixture, seed_users_and_nodes  # noqa: F401


@pytest.fixture(name="events")
def events_fixture(monkeypatch: pytest.MonkeyPatch, recorder_db) -> dict[str, list]:
    events: dict[str, list] = {"pushed": [], "status": [], "next_plan": []}

    async def record_push(users):
        events["pushed"].append([(user.id, user.status) for user in users])

    async def record_status_change(db_user, status):
        events["status"].append((db_user.id, status))

    async def record_next_plan(db_user):
        events["next_plan"].append(db_user.id)

    monkeypatch.setattr(review_users, "sync_users", record_push)
    monkeypatch.setattr(review_users, "_notify_status_change", record_status_change)
    monkeypatch.setattr(review_users, "_notify_next_plan", record_next_plan)
    monkeypatch.setattr(status_review, "GetDB", record_usages.GetDB)
    return events


@pytest.mark.asyncio
async def test_the_expire_job_does_not_announce_a_next_plan_that_was_deleted_under_it(
    monkeypatch: pytest.MonkeyPatch, recorder_db, events
):
    _, user_ids, _ = await seed_users_and_nodes(recorder_db, user_count=1, node_count=0)
    user_id = user_ids[0]
    async with recorder_db() as session:
        group = Group(name="adv-r1a-vanish", inbounds=[])
        session.add(group)
        await session.flush()
        template = UserTemplate(
            name="adv-r1a-vanish",
            username_prefix=None,
            username_suffix=None,
            extra_settings=None,
            groups=[group],
            data_limit=1024**3,
            expire_duration=86400,
        )
        session.add(template)
        await session.flush()
        template_id = template.id
        await session.execute(
            update(User)
            .where(User.id == user_id)
            .values(status=UserStatus.active, expire=datetime.now(UTC) - timedelta(minutes=5))
        )
        session.add(NextPlan(user_id=user_id, user_template_id=template_id))
        await session.commit()

    real_refresh = user_crud.refresh_user_state_for_update
    deleted = []

    async def refresh_then_template_is_deleted(db, db_user, *args, **kwargs):
        found = await real_refresh(db, db_user, *args, **kwargs)
        if db_user.id == user_id and not deleted:
            async with recorder_db() as admin_session:
                await remove_user_templates(admin_session, [template_id])
            deleted.append(template_id)
        return found

    monkeypatch.setattr(user_crud, "refresh_user_state_for_update", refresh_then_template_is_deleted)

    await status_review.expire_users_under_lock([user_id])

    async with recorder_db() as session:
        status = (await session.execute(select(User.status).where(User.id == user_id))).scalar_one()
    assert deleted == [template_id]
    assert status == UserStatus.expired, status
    assert events["status"] == [(user_id, UserStatus.expired)], events
    assert events["next_plan"] == [], (
        f"the template behind user {user_id}'s next plan was deleted after the expire job locked the user; the plan "
        f"was not applied (status stays {status.value}) yet the job pushed {events['pushed']} and announced "
        f"'next plan activated' for {events['next_plan']}"
    )


@pytest.mark.asyncio
async def test_the_limit_path_limits_instead_of_announcing_a_next_plan_that_was_deleted_under_it(
    monkeypatch: pytest.MonkeyPatch, recorder_db, events
):
    from app.fork.jobs import usage_limits

    monkeypatch.setattr(usage_limits, "GetDB", record_usages.GetDB)
    _, user_ids, _ = await seed_users_and_nodes(recorder_db, user_count=1, node_count=0)
    user_id = user_ids[0]
    async with recorder_db() as session:
        await session.execute(
            update(User).where(User.id == user_id).values(status=UserStatus.active, data_limit=1000, used_traffic=1100)
        )
        session.add(NextPlan(user_id=user_id, user_template_id=None, data_limit=5000, expire=86400))
        await session.commit()

    real_refresh = user_crud.refresh_user_state_for_update
    removed = []

    async def refresh_then_plan_is_removed(db, db_user, *args, **kwargs):
        found = await real_refresh(db, db_user, *args, **kwargs)
        if db_user.id == user_id and not removed:
            async with recorder_db() as admin_session:
                await admin_session.execute(delete(NextPlan).where(NextPlan.user_id == user_id))
                await admin_session.commit()
            removed.append(user_id)
        return found

    monkeypatch.setattr(user_crud, "refresh_user_state_for_update", refresh_then_plan_is_removed)

    await usage_limits.enforce_user_limits_now([user_id])

    async with recorder_db() as session:
        row = (
            await session.execute(select(User.status, User.data_limit, User.used_traffic).where(User.id == user_id))
        ).one()
    assert removed == [user_id]
    assert (row.status, row.data_limit, row.used_traffic) == (UserStatus.limited, 1000, 1100), row
    assert events["next_plan"] == [], events
    assert events["status"] == [(user_id, UserStatus.limited)], events
    assert events["pushed"] == [[(user_id, UserStatus.limited)]], events
