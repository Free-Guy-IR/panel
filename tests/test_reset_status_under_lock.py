from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from app.db.models import DataLimitResetStrategy, User, UserStatus
from app.fork.jobs import usage_limits
from app.jobs import record_usages, reset_user_data_usage, review_users
from tests.test_recorder_harness import recorder_db_fixture, seed_users_and_nodes  # noqa: F401


class _SilentNotification:
    def __getattr__(self, name):
        async def _noop(*args, **kwargs):
            return None

        return _noop


@pytest.fixture(name="reset_job")
def reset_job_fixture(monkeypatch: pytest.MonkeyPatch, recorder_db) -> list:
    pushed: list = []

    async def record_push(users):
        pushed.append([(user.id, user.status) for user in users])

    async def validate_user(db_user):
        return SimpleNamespace(id=db_user.id, status=db_user.status, username=db_user.username)

    monkeypatch.setattr(reset_user_data_usage, "GetDB", record_usages.GetDB)
    monkeypatch.setattr(reset_user_data_usage, "sync_users", record_push)
    monkeypatch.setattr(reset_user_data_usage, "notification", _SilentNotification())
    monkeypatch.setattr(reset_user_data_usage.user_operator, "validate_user", validate_user)
    monkeypatch.setattr(review_users, "sync_users", record_push)

    async def quiet_status_change(db_user, status):
        return None

    monkeypatch.setattr(review_users, "_notify_status_change", quiet_status_change)
    return pushed


@pytest.mark.asyncio
async def test_periodic_reset_reactivates_a_user_the_recorder_limited_after_the_job_read(
    monkeypatch: pytest.MonkeyPatch, recorder_db, reset_job
):
    _, user_ids, _ = await seed_users_and_nodes(recorder_db, user_count=1, node_count=0)
    user_id = user_ids[0]
    async with recorder_db() as session:
        await session.execute(
            update(User)
            .where(User.id == user_id)
            .values(
                status=UserStatus.active,
                data_limit=1000,
                used_traffic=990,
                data_limit_reset_strategy=DataLimitResetStrategy.day,
                created_at=datetime.now(UTC) - timedelta(days=2),
            )
        )
        await session.commit()

    real_candidates = reset_user_data_usage.get_users_to_reset_data_usage

    async def candidates_then_tick_limits(db):
        due = await real_candidates(db)
        async with recorder_db() as recorder_session:
            await recorder_session.execute(
                update(User).where(User.id == user_id).values(used_traffic=User.used_traffic + 20)
            )
            await recorder_session.commit()
        await usage_limits.enforce_user_limits_now([user_id])
        return due

    monkeypatch.setattr(reset_user_data_usage, "get_users_to_reset_data_usage", candidates_then_tick_limits)

    await reset_user_data_usage.reset_data_usage()

    async with recorder_db() as session:
        row = (await session.execute(select(User.status, User.used_traffic).where(User.id == user_id))).one()
    assert row.used_traffic == 0
    assert row.status == UserStatus.active, (row, reset_job)
