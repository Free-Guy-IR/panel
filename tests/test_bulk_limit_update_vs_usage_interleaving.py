from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select, update

from app.db.crud.bulk import BulkUserUpdateConflict, get_bulk_user_limit_targets, update_users_datalimit
from app.db.models import User, UserStatus
from app.models.user import BulkUser
from tests.test_next_plan_reset_lock_order import server_database_only
from tests.test_recorder_harness import recorder_db_fixture, seed_users_and_nodes  # noqa: F401

MB = 1024**2
START_LIMIT = 5 * MB
START_USED = 1 * MB
AMOUNT = 2 * MB
USAGE = 3 * MB
ROLE_MAX = 10 * MB


async def _seed(recorder_db) -> list[int]:
    _, user_ids, _ = await seed_users_and_nodes(recorder_db, user_count=2, node_count=0)
    async with recorder_db() as session:
        await session.execute(
            update(User)
            .where(User.id.in_(user_ids))
            .values(status=UserStatus.active, data_limit=START_LIMIT, used_traffic=START_USED)
        )
        await session.commit()
    return user_ids


async def _record_usage(recorder_db, user_ids: list[int], value: int) -> None:
    async with recorder_db() as session:
        for user_id in user_ids:
            await session.execute(
                update(User)
                .where(User.id == user_id)
                .values(used_traffic=User.used_traffic + value)
                .execution_options(synchronize_session=False)
            )
        await session.commit()


async def _rows(recorder_db, user_ids: list[int]) -> dict[int, tuple[int, int]]:
    async with recorder_db() as session:
        rows = await session.execute(select(User.id, User.data_limit, User.used_traffic).where(User.id.in_(user_ids)))
        return {user_id: (data_limit, used) for user_id, data_limit, used in rows.all()}


def _validated(targets: list[tuple], amount: int) -> list[int]:
    assert all(value + amount <= ROLE_MAX for _, value in targets), targets
    return [user_id for user_id, _ in targets]


@pytest.mark.asyncio
async def test_usage_recorded_between_bulk_validation_and_write_is_kept_and_the_quota_is_not_overwritten(
    recorder_db,
):
    user_ids = await _seed(recorder_db)
    bulk = BulkUser(amount=AMOUNT, users=user_ids)
    async with recorder_db() as session:
        targets = await get_bulk_user_limit_targets(session, bulk, "data_limit", lock=False)
        await _record_usage(recorder_db, user_ids, USAGE)
        await update_users_datalimit(session, bulk, target_ids=_validated(targets, AMOUNT), maximum=ROLE_MAX)

    rows = await _rows(recorder_db, user_ids)
    assert rows == {user_id: (START_LIMIT + AMOUNT, START_USED + USAGE) for user_id in user_ids}, rows


@pytest.mark.asyncio
async def test_a_quota_raised_between_bulk_validation_and_write_cannot_push_the_batch_past_the_role_limit(
    recorder_db,
):
    user_ids = await _seed(recorder_db)
    bulk = BulkUser(amount=AMOUNT, users=user_ids)
    async with recorder_db() as session:
        targets = await get_bulk_user_limit_targets(session, bulk, "data_limit", lock=False)
        async with recorder_db() as other:
            await other.execute(update(User).where(User.id == user_ids[1]).values(data_limit=ROLE_MAX - MB))
            await other.commit()
        await _record_usage(recorder_db, user_ids, USAGE)
        with pytest.raises(BulkUserUpdateConflict):
            await update_users_datalimit(session, bulk, target_ids=_validated(targets, AMOUNT), maximum=ROLE_MAX)

    rows = await _rows(recorder_db, user_ids)
    assert rows == {
        user_ids[0]: (START_LIMIT, START_USED + USAGE),
        user_ids[1]: (ROLE_MAX - MB, START_USED + USAGE),
    }, rows


@server_database_only
@pytest.mark.asyncio
@pytest.mark.parametrize("attempt", range(3))
async def test_usage_recording_waits_for_the_locked_bulk_quota_update_and_both_land(recorder_db, attempt):
    user_ids = await _seed(recorder_db)
    bulk = BulkUser(amount=AMOUNT, users=user_ids)
    async with recorder_db() as session:
        targets = await get_bulk_user_limit_targets(session, bulk, "data_limit", lock=True)
        usage = asyncio.create_task(_record_usage(recorder_db, user_ids, USAGE))
        await asyncio.sleep(1.0)
        assert not usage.done(), "the usage write did not wait for the bulk lock"
        await update_users_datalimit(session, bulk, target_ids=_validated(targets, AMOUNT), maximum=ROLE_MAX)
    await asyncio.wait_for(usage, timeout=60)

    rows = await _rows(recorder_db, user_ids)
    assert rows == {user_id: (START_LIMIT + AMOUNT, START_USED + USAGE) for user_id in user_ids}, rows


@server_database_only
@pytest.mark.asyncio
@pytest.mark.parametrize("attempt", range(3))
async def test_bulk_quota_update_and_usage_recording_started_together_lose_nothing(recorder_db, attempt):
    user_ids = await _seed(recorder_db)
    bulk = BulkUser(amount=AMOUNT, users=user_ids)

    async def bulk_update():
        async with recorder_db() as session:
            targets = await get_bulk_user_limit_targets(session, bulk, "data_limit", lock=True)
            await update_users_datalimit(session, bulk, target_ids=_validated(targets, AMOUNT), maximum=ROLE_MAX)

    outcomes = await asyncio.wait_for(
        asyncio.gather(bulk_update(), _record_usage(recorder_db, user_ids, USAGE), return_exceptions=True),
        timeout=90,
    )

    assert not [repr(outcome) for outcome in outcomes if isinstance(outcome, BaseException)], outcomes
    rows = await _rows(recorder_db, user_ids)
    assert rows == {user_id: (START_LIMIT + AMOUNT, START_USED + USAGE) for user_id in user_ids}, rows
