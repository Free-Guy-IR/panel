from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from app.db.crud.bulk import (
    BulkUserUpdateConflict,
    bulk_out_of_range_target_exists,
    get_bulk_user_limit_targets,
    update_users_datalimit,
    update_users_expire,
)
from app.db.models import User, UserStatus
from app.fork.crud.bulk_result_bounds import BulkUserResultOutOfRange
from app.models.user import BulkUser
from tests.test_next_plan_reset_lock_order import server_database_only
from tests.test_recorder_harness import recorder_db_fixture, seed_users_and_nodes  # noqa: F401

MB = 1024**2
START_LIMIT = 5 * MB
START_USED = 1 * MB
AMOUNT = 2 * MB
USAGE = 3 * MB
ROLE_MAX = 10 * MB
BIGINT_MAX = 2**63 - 1
BIG_STEP = 2**62
FAR_FUTURE = datetime(9800, 1, 1, tzinfo=UTC)


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


async def _set_user(recorder_db, user_id: int, **values) -> None:
    async with recorder_db() as session:
        await session.execute(update(User).where(User.id == user_id).values(**values))
        await session.commit()


async def _expires(recorder_db, user_ids: list[int]) -> dict[int, datetime | None]:
    async with recorder_db() as session:
        rows = await session.execute(select(User.id, User.expire).where(User.id.in_(user_ids)))
        return {
            user_id: expire.replace(tzinfo=UTC) if expire is not None and expire.tzinfo is None else expire
            for user_id, expire in rows.all()
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("strict", [False, True])
async def test_a_quota_pushed_into_overflow_range_after_the_bulk_check_is_rejected_and_usage_is_kept(
    recorder_db, strict
):
    user_ids = await _seed(recorder_db)
    bulk = BulkUser(amount=BIG_STEP, users=user_ids)
    async with recorder_db() as session:
        assert await bulk_out_of_range_target_exists(session, bulk, "data_limit") is False
        await _set_user(recorder_db, user_ids[1], data_limit=BIG_STEP)
        await _record_usage(recorder_db, user_ids, USAGE)
        with pytest.raises(BulkUserResultOutOfRange):
            await update_users_datalimit(session, bulk, target_ids=user_ids if strict else None)

    rows = await _rows(recorder_db, user_ids)
    assert rows == {
        user_ids[0]: (START_LIMIT, START_USED + USAGE),
        user_ids[1]: (BIG_STEP, START_USED + USAGE),
    }, rows


@pytest.mark.asyncio
@pytest.mark.parametrize("strict", [False, True])
async def test_an_expire_pushed_into_overflow_range_after_the_bulk_check_is_rejected_and_never_nulled(
    recorder_db, strict
):
    user_ids = await _seed(recorder_db)
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=2)
    for user_id in user_ids:
        await _set_user(recorder_db, user_id, expire=start)
    bulk = BulkUser(amount=10**10, users=user_ids)
    async with recorder_db() as session:
        assert await bulk_out_of_range_target_exists(session, bulk, "expire") is False
        await _set_user(recorder_db, user_ids[1], expire=FAR_FUTURE)
        await _record_usage(recorder_db, user_ids, USAGE)
        with pytest.raises(BulkUserResultOutOfRange):
            await update_users_expire(session, bulk, target_ids=user_ids if strict else None)

    assert await _expires(recorder_db, user_ids) == {user_ids[0]: start, user_ids[1]: FAR_FUTURE}
    assert {used for _, used in (await _rows(recorder_db, user_ids)).values()} == {START_USED + USAGE}


@server_database_only
@pytest.mark.asyncio
@pytest.mark.parametrize("attempt", range(3))
async def test_bulk_quota_step_racing_a_quota_raise_and_usage_recording_never_stores_past_bigint(recorder_db, attempt):
    user_ids = await _seed(recorder_db)
    bulk = BulkUser(amount=BIG_STEP, users=user_ids)
    raised = BIG_STEP + 1

    async def bulk_update():
        async with recorder_db() as session:
            await update_users_datalimit(session, bulk)

    outcomes = await asyncio.wait_for(
        asyncio.gather(
            bulk_update(),
            _set_user(recorder_db, user_ids[1], data_limit=raised),
            _record_usage(recorder_db, user_ids, USAGE),
            return_exceptions=True,
        ),
        timeout=90,
    )

    unexpected = [
        repr(outcome)
        for outcome in outcomes
        if isinstance(outcome, BaseException) and not isinstance(outcome, BulkUserResultOutOfRange)
    ]
    assert not unexpected, outcomes
    rows = await _rows(recorder_db, user_ids)
    assert all(0 <= data_limit <= BIGINT_MAX for data_limit, _ in rows.values()), rows
    assert {used for _, used in rows.values()} == {START_USED + USAGE}, rows
    if isinstance(outcomes[0], BulkUserResultOutOfRange):
        assert rows[user_ids[0]][0] == START_LIMIT and rows[user_ids[1]][0] == raised, rows
    else:
        assert rows[user_ids[0]][0] == START_LIMIT + BIG_STEP and rows[user_ids[1]][0] == raised, rows


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", [60, -60])
@pytest.mark.parametrize("strict", [False, True])
async def test_a_user_already_loaded_in_the_bulk_session_keeps_the_expire_step_working(recorder_db, amount, strict):
    user_ids = await _seed(recorder_db)
    start = datetime(2030, 1, 1, tzinfo=UTC)
    for user_id in user_ids:
        await _set_user(recorder_db, user_id, expire=start)
    async with recorder_db() as session:
        loaded = (await session.execute(select(User).where(User.id == user_ids[0]))).scalar_one()
        assert loaded.id == user_ids[0]
        _, count = await update_users_expire(
            session, BulkUser(amount=amount, users=user_ids), target_ids=user_ids if strict else None
        )
    assert count == 2
    expected = start + timedelta(seconds=amount)
    assert await _expires(recorder_db, user_ids) == {user_id: expected for user_id in user_ids}
