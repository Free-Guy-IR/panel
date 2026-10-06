from __future__ import annotations

import asyncio
import itertools
import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select, text, update
from sqlalchemy.orm import Session

from app.db.crud.user import get_active_to_expire_users, get_active_to_limited_users, get_user_by_id
from app.db.models import ConnectionRestriction, Group, ProxyInbound, User, UserStatus, users_groups_association
from app.fork.jobs import connection_limiter, status_review
from app.fork.proxy_secrets import entitled as entitled_module, locks as locks_module
from app.fork.proxy_secrets.locks import LOCK_UNAVAILABLE_DETAIL, PROXY_SECRETS_LOCK, acquire_named_lock
from app.jobs import review_users
from app.models.proxy import ProxyTable
from app.models.settings import ConnectionLimit
from app.models.user import UserModify
from app.operation import OperatorType, user as user_operation
from app.operation.user import UserOperation
from tests.test_next_plan_reset_lock_order import (  # noqa: F401
    OWNER,
    _deadlock,
    _lock_wait_timeout,
    _plan_left,
    _seed,
    quiet,
    server_database_only,
)
from tests.test_recorder_harness import recorder_db_fixture  # noqa: F401

REAL_UPDATE_USER = UserOperation.update_user
JOBS = {"expire_job": review_users.expire_users_job, "limit_job": review_users.limit_users_job}
LIMITER = ConnectionLimit(
    enabled=True, monitor_only=False, enforcement_enabled=True, persistence_cycles=1, punishment_steps=[30]
)


async def _row(recorder_db, user_id: int) -> dict:
    async with recorder_db() as session:
        user = (await session.execute(select(User).where(User.id == user_id))).unique().scalar_one()
        table = ProxyTable.model_validate(user.proxy_settings)
        groups = sorted(
            (await session.execute(select(Group.name).join(Group.users).where(User.id == user_id).order_by(Group.name)))
            .scalars()
            .all()
        )
        return {
            "status": user.status,
            "expire": user.expire,
            "data_limit": user.data_limit,
            "used_traffic": user.used_traffic,
            "groups": groups,
            "vless": str(table.vless.id),
            "vmess": str(table.vmess.id),
            "openvpn": table.openvpn.password,
        }


def _pushed_ids(sync_users_mock) -> list[int]:
    return [user.id for call in sync_users_mock.await_args_list for user in call.args[0]]


def _hold_the_lock_in_run(monkeypatch, runs: set[int]) -> list[int]:
    real = locks_module.acquire_named_lock
    current_run = [0]

    async def held_during_some_runs(db, name):
        if name == PROXY_SECRETS_LOCK and current_run[0] in runs:
            raise _lock_wait_timeout()
        return await real(db, name)

    monkeypatch.setattr(locks_module, "acquire_named_lock", held_during_some_runs)
    return current_run


@pytest.mark.asyncio
@pytest.mark.parametrize("path", sorted(JOBS))
async def test_a_postponed_renewal_is_applied_with_its_secrets_and_pushed_on_the_next_run(
    monkeypatch, recorder_db, path
):
    renewing, _ = await _seed(recorder_db, path)
    before = await _row(recorder_db, renewing)
    run = _hold_the_lock_in_run(monkeypatch, {1})

    run[0] = 1
    await JOBS[path]()
    postponed = await _row(recorder_db, renewing)
    assert await _plan_left(recorder_db, renewing)
    assert (postponed["status"], postponed["openvpn"], postponed["groups"]) == (
        UserStatus.active,
        None,
        before["groups"],
    )
    assert renewing not in _pushed_ids(review_users.sync_users)

    run[0] = 2
    await JOBS[path]()
    applied = await _row(recorder_db, renewing)
    assert not await _plan_left(recorder_db, renewing)
    assert applied["status"] == UserStatus.active
    assert applied["openvpn"], "the renewal was applied without the OpenVPN secret its template's group entitles"
    assert applied["groups"] != before["groups"] and applied["groups"]
    assert (applied["vless"], applied["vmess"]) == (before["vless"], before["vmess"])
    assert applied["data_limit"] == 5 * 1024**3 and applied["used_traffic"] == 0
    assert renewing in _pushed_ids(review_users.sync_users)


@server_database_only
@pytest.mark.asyncio
@pytest.mark.parametrize("path", sorted(JOBS))
async def test_a_renewal_that_waited_out_a_real_lock_holder_is_applied_on_the_next_run(monkeypatch, recorder_db, path):
    renewing, _ = await _seed(recorder_db, path)
    real = locks_module.acquire_named_lock

    async def short_wait(db, name):
        dialect = db.get_bind().dialect.name
        statement = "SET lock_timeout = '1s'" if dialect == "postgresql" else "SET SESSION innodb_lock_wait_timeout = 1"
        await db.execute(text(statement))
        return await real(db, name)

    monkeypatch.setattr(locks_module, "acquire_named_lock", short_wait)

    async with recorder_db() as holder:
        await acquire_named_lock(holder, PROXY_SECRETS_LOCK)
        await JOBS[path]()
        assert await _plan_left(recorder_db, renewing)
        await holder.rollback()

    await JOBS[path]()
    applied = await _row(recorder_db, renewing)
    assert not await _plan_left(recorder_db, renewing)
    assert (applied["status"], bool(applied["openvpn"])) == (UserStatus.active, True)
    assert renewing in _pushed_ids(review_users.sync_users)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", sorted(JOBS))
async def test_a_renewal_postponed_on_five_runs_logs_an_error_and_stays_eligible(
    monkeypatch, recorder_db, caplog, path
):
    renewing, _ = await _seed(recorder_db, path)
    run = _hold_the_lock_in_run(monkeypatch, {1, 2, 3, 4, 5})
    candidates = get_active_to_expire_users if path == "expire_job" else get_active_to_limited_users

    for number in range(1, 6):
        run[0] = number
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            await JOBS[path]()
        mine = [record for record in caplog.records if str(renewing) in record.getMessage()]
        assert mine, f"run {number} postponed user {renewing} silently"
        errors = [record for record in mine if record.levelno >= logging.ERROR]
        if number < status_review.NEXT_PLAN_POSTPONEMENTS_BEFORE_ERROR:
            assert not errors, [record.getMessage() for record in errors]
        else:
            assert errors, f"run {number} postponed user {renewing} again without an ERROR"
            assert PROXY_SECRETS_LOCK in errors[-1].getMessage(), errors[-1].getMessage()
        async with recorder_db() as session:
            assert renewing in [user.id for user in await candidates(session)], f"run {number} dropped the user"
        assert await _plan_left(recorder_db, renewing)
        assert (await _row(recorder_db, renewing))["status"] == UserStatus.active

    run[0] = 6
    await JOBS[path]()
    assert not await _plan_left(recorder_db, renewing)
    assert renewing not in status_review._postponed_renewals


def _fail_on_the_first_user_flush(error):
    raised: list[bool] = []

    def after_flush(session, _context):
        if raised or not any(isinstance(obj, User) for obj in session.dirty):
            return
        raised.append(True)
        raise error

    event.listen(Session, "after_flush", after_flush)
    return raised, lambda: event.remove(Session, "after_flush", after_flush)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [_deadlock, _lock_wait_timeout], ids=["deadlock", "lock-wait-timeout"])
async def test_a_bot_renewal_put_that_loses_a_lock_answers_503_and_writes_nothing(monkeypatch, recorder_db, error):
    renewing, _ = await _seed(recorder_db, "expire_job", template=False)
    before = await _row(recorder_db, renewing)
    renewal = UserModify(expire=datetime.now(UTC) + timedelta(days=30), data_limit=7 * 1024**3)

    raised, stop = _fail_on_the_first_user_flush(error())
    try:
        async with recorder_db() as db:
            db_user = await get_user_by_id(db, renewing, load_admin_role=True)
            with pytest.raises(HTTPException) as answer:
                await UserOperation(OperatorType.API)._modify_user(db, db_user, renewal, OWNER, skip_role_limits=True)
    finally:
        stop()

    assert raised, "the injected lock error never fired inside the write"
    assert (answer.value.status_code, answer.value.detail) == (503, LOCK_UNAVAILABLE_DETAIL)
    assert await _row(recorder_db, renewing) == before

    async with recorder_db() as db:
        db_user = await get_user_by_id(db, renewing, load_admin_role=True)
        await UserOperation(OperatorType.API)._modify_user(db, db_user, renewal, OWNER, skip_role_limits=True)
    retried = await _row(recorder_db, renewing)
    assert retried["data_limit"] == 7 * 1024**3
    assert (retried["vless"], retried["vmess"]) == (before["vless"], before["vmess"])


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [_deadlock, _lock_wait_timeout], ids=["deadlock", "lock-wait-timeout"])
async def test_an_admin_next_plan_activation_that_loses_a_lock_after_writing_answers_503_and_writes_nothing(
    monkeypatch, recorder_db, error
):
    renewing, _ = await _seed(recorder_db, "admin_route")
    before = await _row(recorder_db, renewing)

    async def write_then_lose_the_lock(db, db_user):
        await db.flush()
        raise error()

    monkeypatch.setattr(entitled_module, "grant_entitled_secrets_after_plan_change", write_then_lose_the_lock)

    async with recorder_db() as db:
        db_user = await get_user_by_id(db, renewing)
        with pytest.raises(HTTPException) as answer:
            await UserOperation(OperatorType.API)._active_next_plan(db, db_user, OWNER)

    assert (answer.value.status_code, answer.value.detail) == (503, LOCK_UNAVAILABLE_DETAIL)
    assert await _row(recorder_db, renewing) == before
    assert await _plan_left(recorder_db, renewing)


async def _limiter_disabled_and_due(recorder_db, path: str) -> tuple[int, dict]:
    renewing, _ = await _seed(recorder_db, path)
    imposed = datetime.now(UTC) - timedelta(minutes=5)
    async with recorder_db() as session:
        inbound = ProxyInbound(tag=f"vless-{uuid4().hex[:8]}")
        group = Group(name=inbound.tag, inbounds=[inbound])
        session.add_all([inbound, group])
        await session.flush()
        await session.execute(users_groups_association.insert().values(user_id=renewing, groups_id=group.id))
        await session.execute(
            update(User)
            .where(User.id == renewing)
            .values(status=UserStatus.disabled, last_status_change=imposed - timedelta(seconds=1))
        )
        session.add(
            ConnectionRestriction(
                user_id=renewing,
                previous_status=UserStatus.active.value,
                created_at=imposed,
                disable_minutes=30,
                restore_at=datetime.now(UTC) - timedelta(seconds=1),
                active=True,
            )
        )
        await session.commit()
    return renewing, await _row(recorder_db, renewing)


def _record_pushes(monkeypatch) -> list[tuple[str, int, UserStatus, str, str]]:
    pushes: list[tuple[str, int, UserStatus, str, str]] = []

    def entry(source, user):
        table = ProxyTable.model_validate(user.proxy_settings)
        return (source, user.id, user.status, str(table.vless.id), str(table.vmess.id))

    async def one(db_user):
        pushes.append(entry("single", db_user))

    async def many(users):
        pushes.extend(entry("job", user) for user in users)

    monkeypatch.setattr(UserOperation, "update_user", REAL_UPDATE_USER)
    monkeypatch.setattr(user_operation, "sync_user", one)
    monkeypatch.setattr(review_users, "sync_users", many)
    return pushes


async def _bot_put(recorder_db, user_id: int) -> None:
    async with recorder_db() as db:
        db_user = await get_user_by_id(db, user_id, load_admin_role=True)
        await UserOperation(OperatorType.API)._modify_user(
            db,
            db_user,
            UserModify(expire=datetime.now(UTC) + timedelta(days=30), data_limit=9 * 1024**3, status=UserStatus.active),
            OWNER,
            skip_role_limits=True,
        )


async def _release(recorder_db) -> None:
    async with recorder_db() as db:
        await connection_limiter._release_due(db, LIMITER)


def _assert_consistent(before: dict, after: dict, pushes: list, user_id: int) -> None:
    assert after["status"] == UserStatus.active, (after, pushes)
    assert (after["vless"], after["vmess"]) == (before["vless"], before["vmess"]), (before, after)
    mine = [push for push in pushes if push[1] == user_id]
    assert mine, "nothing was pushed for the renewed user"
    assert mine[-1][2:] == (after["status"], after["vless"], after["vmess"]), (mine, after)


STEPS = ("job", "put", "release")


@pytest.mark.asyncio
@pytest.mark.parametrize("order", list(itertools.permutations(STEPS)), ids="-".join)
async def test_the_renewal_job_a_bot_put_and_the_limiter_release_in_any_order_end_active_with_the_same_ids(
    monkeypatch, recorder_db, order
):
    renewing, before = await _limiter_disabled_and_due(recorder_db, "expire_job")
    pushes = _record_pushes(monkeypatch)
    monkeypatch.setattr(connection_limiter, "_push_to_nodes", _push_through_the_operator)
    steps = {
        "job": review_users.expire_users_job,
        "put": lambda: _bot_put(recorder_db, renewing),
        "release": lambda: _release(recorder_db),
    }

    for step in order:
        await steps[step]()

    _assert_consistent(before, await _row(recorder_db, renewing), pushes, renewing)


async def _push_through_the_operator(users) -> None:
    for user in users:
        await UserOperation(OperatorType.API).update_user(user)


@server_database_only
@pytest.mark.asyncio
@pytest.mark.parametrize("attempt", range(3))
async def test_the_renewal_job_a_bot_put_and_the_limiter_release_started_together_end_active_with_the_same_ids(
    monkeypatch, recorder_db, attempt
):
    renewing, before = await _limiter_disabled_and_due(recorder_db, "expire_job")
    pushes = _record_pushes(monkeypatch)
    monkeypatch.setattr(connection_limiter, "_push_to_nodes", _push_through_the_operator)

    outcomes = await asyncio.wait_for(
        asyncio.gather(
            _release(recorder_db),
            review_users.expire_users_job(),
            _bot_put(recorder_db, renewing),
            return_exceptions=True,
        ),
        timeout=90,
    )

    assert not [repr(outcome) for outcome in outcomes if isinstance(outcome, BaseException)], outcomes
    _assert_consistent(before, await _row(recorder_db, renewing), pushes, renewing)


@server_database_only
@pytest.mark.asyncio
async def test_a_bot_put_arriving_while_the_renewal_holds_its_locks_waits_and_both_land(monkeypatch, recorder_db):
    renewing, before = await _limiter_disabled_and_due(recorder_db, "expire_job")
    monkeypatch.setattr(connection_limiter, "_push_to_nodes", _push_through_the_operator)
    await _release(recorder_db)
    pushes = _record_pushes(monkeypatch)
    inside = asyncio.Event()
    proceed = asyncio.Event()
    real_reset = review_users.reset_user_by_next

    async def hold_the_renewal(db, db_user, **kwargs):
        inside.set()
        await proceed.wait()
        return await real_reset(db, db_user, **kwargs)

    monkeypatch.setattr(review_users, "reset_user_by_next", hold_the_renewal)
    job = asyncio.create_task(review_users.expire_users_job())
    await asyncio.wait_for(inside.wait(), timeout=30)
    put = asyncio.create_task(_bot_put(recorder_db, renewing))
    release = asyncio.create_task(_release(recorder_db))
    await asyncio.sleep(1.5)
    assert not put.done(), "the bot PUT did not wait for the renewal's named lock"
    proceed.set()
    outcomes = await asyncio.wait_for(asyncio.gather(job, put, release, return_exceptions=True), timeout=90)

    assert not [repr(outcome) for outcome in outcomes if isinstance(outcome, BaseException)], outcomes
    after = await _row(recorder_db, renewing)
    _assert_consistent(before, after, pushes, renewing)
    assert not await _plan_left(recorder_db, renewing)
    assert (after["data_limit"], bool(after["openvpn"])) == (9 * 1024**3, True), after
