import pytest
from sqlalchemy import select

from app.db.crud.user import reset_user_by_next
from app.db.models import NextPlan, User, UserStatus, UserUsageResetLogs
from tests.test_record_usages import (  # noqa: F401
    _commit_external_usage,
    _read_usage_epochs,
    _seed_billing_fixture,
    reset_user_usage_history_buffer,
    session_factory,
)


@pytest.mark.asyncio
async def test_a_next_plan_reset_bumps_the_usage_epoch_and_logs_committed_traffic(session_factory):  # noqa: F811
    _, user_ids, _ = await _seed_billing_fixture(
        session_factory,
        node_coefficients=[],
        user_names=["next-plan-epoch"],
        admin_names=["next-plan-epoch-admin"],
    )
    user_id = user_ids[0]
    async with session_factory() as session:
        session.add(NextPlan(user_id=user_id, user_template_id=None, data_limit=10_000, expire=0))
        await session.commit()
    before = await _read_usage_epochs(session_factory, [user_id])

    async with session_factory() as session:
        db_user = (await session.execute(select(User).where(User.id == user_id))).unique().scalar_one()
        await session.commit()
        await _commit_external_usage(session_factory, user_id, 700)
        await reset_user_by_next(session, db_user)

    after = await _read_usage_epochs(session_factory, [user_id])
    assert after[user_id] == before[user_id] + 1
    async with session_factory() as session:
        row = (await session.execute(select(User).where(User.id == user_id))).unique().scalar_one()
        logs = (
            (
                await session.execute(
                    select(UserUsageResetLogs.used_traffic_at_reset).where(UserUsageResetLogs.user_id == user_id)
                )
            )
            .scalars()
            .all()
        )
        plans = (await session.execute(select(NextPlan.id).where(NextPlan.user_id == user_id))).scalars().all()
    assert (row.used_traffic, row.data_limit, row.status) == (0, 10_000, UserStatus.active)
    assert logs == [700]
    assert plans == []
