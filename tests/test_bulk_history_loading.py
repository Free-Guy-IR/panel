from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import inspect, update

from app.db.models import User, UserUsageResetLogs
from app.operation import OperatorType
from app.operation.user import UserOperation
from tests.test_recorder_harness import recorder_db_fixture, seed_users_and_nodes  # noqa: F401

OWNER = SimpleNamespace(username="owner", is_owner=True, role=None, id=None, permission_overrides=None)
RESET = 700
USED = 300


async def _seed(recorder_db) -> list[int]:
    _, user_ids, _ = await seed_users_and_nodes(recorder_db, user_count=2, node_count=0)
    async with recorder_db() as session:
        await session.execute(update(User).where(User.id.in_(user_ids)).values(used_traffic=USED))
        session.add_all(UserUsageResetLogs(user_id=user_id, used_traffic_at_reset=RESET) for user_id in user_ids)
        await session.commit()
    return user_ids


@pytest.mark.asyncio
async def test_bulk_validation_without_history_skips_reset_rows_and_keeps_lifetime_usage(recorder_db):
    user_ids = await _seed(recorder_db)
    async with recorder_db() as session:
        users = await UserOperation(OperatorType.API)._get_validated_users_by_ids(
            session, user_ids, OWNER, load_usage_logs=False
        )

        assert sorted(user.id for user in users) == sorted(user_ids)
        for user in users:
            assert "usage_logs" in inspect(user).unloaded, user.id
            assert user.lifetime_used_traffic == RESET + USED, user.id


@pytest.mark.asyncio
async def test_bulk_validation_with_history_still_loads_reset_rows(recorder_db):
    user_ids = await _seed(recorder_db)
    async with recorder_db() as session:
        users = await UserOperation(OperatorType.API)._get_validated_users_by_ids(session, user_ids, OWNER)

        for user in users:
            assert "usage_logs" not in inspect(user).unloaded, user.id
            assert user.lifetime_used_traffic == RESET + USED, user.id


@pytest.mark.asyncio
async def test_bulk_result_reload_refreshes_aggregates_without_materializing_history(recorder_db):
    user_ids = await _seed(recorder_db)
    operation = UserOperation(OperatorType.API)
    async with recorder_db() as session:
        await operation._get_validated_users_by_ids(session, user_ids, OWNER, load_usage_logs=False)
        await session.execute(update(User).where(User.id.in_(user_ids)).values(used_traffic=USED + 50))
        session.add_all(UserUsageResetLogs(user_id=user_id, used_traffic_at_reset=RESET) for user_id in user_ids)
        await session.commit()

        users = await operation._load_users_by_ids(session, user_ids)

        assert [user.id for user in users] == user_ids
        for user in users:
            assert "usage_logs" in inspect(user).unloaded, user.id
            assert user.used_traffic == USED + 50, user.id
            assert user.lifetime_used_traffic == 2 * RESET + USED + 50, user.id
