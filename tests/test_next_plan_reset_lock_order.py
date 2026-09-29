from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.sql.util import find_tables

from app import notification
from app.db.crud import user as user_crud
from app.db.crud.user import get_user_by_id
from app.db.models import CoreConfig, Group, NextPlan, ProxyInbound, User, UserStatus, UserTemplate
from app.fork.jobs import status_review, usage_limits
from app.fork.proxy_secrets import entitled as entitled_module
from app.fork.proxy_secrets.locks import LOCK_UNAVAILABLE_DETAIL
from app.jobs import record_usages, review_users
from app.models.core import CoreType
from app.operation import OperatorType, user as user_operation
from app.operation.user import UserOperation
from tests.test_recorder_harness import _database_url, recorder_db_fixture, seed_users_and_nodes  # noqa: F401

OWNER = SimpleNamespace(username="owner", is_owner=True, role=None, id=None)
PATHS = ("reset", "expire_job", "limit_job", "admin_route")
ROW_WORK = (
    "lock users",
    "lock next_plans",
    "lock wireguard_subnets",
    "write users",
    "write users_groups_association",
    "write wireguard_subnets",
    "write next_plans",
)
WRITTEN = {"users", "users_groups_association", "wireguard_subnets", "next_plans"}
server_database_only = pytest.mark.skipif(
    _database_url().startswith("sqlite"), reason="needs a server database with row locks and deadlock detection"
)


class _DriverError(Exception):
    pass


def _deadlock() -> OperationalError:
    return OperationalError(
        "UPDATE users", {}, _DriverError(1213, "Deadlock found when trying to get lock; try restarting transaction")
    )


def _lock_wait_timeout() -> OperationalError:
    return OperationalError(
        "UPDATE named_locks", {}, _DriverError(1205, "Lock wait timeout exceeded; try restarting transaction")
    )


@pytest.fixture(autouse=True)
def quiet(monkeypatch: pytest.MonkeyPatch, recorder_db):
    async def updated(self, db_user):
        return SimpleNamespace(id=db_user.id, username=db_user.username, status=db_user.status)

    async def nothing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(status_review, "GetDB", record_usages.GetDB)
    monkeypatch.setattr(review_users, "sync_users", AsyncMock())
    monkeypatch.setattr(review_users, "_notify_status_change", nothing)
    monkeypatch.setattr(review_users, "_notify_next_plan", nothing)
    monkeypatch.setattr(UserOperation, "update_user", updated)
    monkeypatch.setattr(user_operation, "sync_user", AsyncMock())
    for name in ("user_status_change", "user_data_reset_by_next", "modify_user", "remove_user"):
        monkeypatch.setattr(notification, name, AsyncMock())
    monkeypatch.setattr(UserOperation, "generate_subscription_url", staticmethod(AsyncMock(return_value="")))


async def _seed(recorder_db, path: str, *, template: bool = True, with_wireguard: bool = False) -> tuple[int, int]:
    _, (renewing, bystander), _ = await seed_users_and_nodes(recorder_db, user_count=2, node_count=0)
    tag = uuid4().hex[:8]
    now = datetime.now(UTC)
    async with recorder_db() as session:
        ovpn = ProxyInbound(tag=f"ovpn-{tag}")
        inbounds = [ovpn]
        session.add(ovpn)
        session.add(CoreConfig(name=f"ovpn-{tag}", type=CoreType.openvpn, config={"instances": [{"tag": ovpn.tag}]}))
        if with_wireguard:
            wg = ProxyInbound(tag=f"wg-{tag}")
            inbounds.append(wg)
            session.add(wg)
            session.add(
                CoreConfig(
                    name=wg.tag,
                    type=CoreType.wg,
                    config={"interface_name": wg.tag, "address": ["10.93.0.1/24"], "listen_port": 51820},
                )
            )
        await session.flush()
        group = Group(name=f"vpn-{tag}", inbounds=inbounds)
        session.add(group)
        await session.flush()
        if template:
            plan_template = UserTemplate(
                name=f"plan-{tag}",
                username_prefix=None,
                username_suffix=None,
                extra_settings=None,
                groups=[group],
                data_limit=5 * 1024**3,
                expire_duration=30 * 86400,
            )
            session.add(plan_template)
            await session.flush()
            session.add(NextPlan(user_id=renewing, user_template_id=plan_template.id))
        else:
            session.add(NextPlan(user_id=renewing, user_template_id=None, data_limit=5000, expire=86400))
        values = {"status": UserStatus.active, "data_limit": 1000, "used_traffic": 0, "expire": now + timedelta(days=1)}
        if path == "expire_job":
            values["expire"] = now - timedelta(minutes=5)
        if path == "limit_job":
            values["used_traffic"] = 1000
        await session.execute(update(User).where(User.id == renewing).values(**values))
        if with_wireguard:
            bystander_user = await session.get(User, bystander)
            proxy_settings = dict(bystander_user.proxy_settings or {})
            proxy_settings["wireguard"] = {**(proxy_settings.get("wireguard") or {}), "peer_ips": ["10.93.0.9/32"]}
            bystander_user.proxy_settings = proxy_settings
        await session.commit()
    return renewing, bystander


async def _renew(recorder_db, path: str, user_id: int) -> None:
    if path == "reset":
        async with recorder_db() as session:
            await user_crud.reset_user_by_next(session, await get_user_by_id(session, user_id))
    elif path == "expire_job":
        await status_review.expire_users_under_lock([user_id])
    elif path == "limit_job":
        await usage_limits.limit_users_under_lock([user_id], usage_limits.LIMITABLE_STATUSES)
    else:
        async with recorder_db() as session:
            db_user = await get_user_by_id(session, user_id)
            await UserOperation(OperatorType.API)._active_next_plan(session, db_user, OWNER)


async def _plan_left(recorder_db, user_id: int) -> bool:
    async with recorder_db() as session:
        return bool((await session.execute(select(NextPlan.id).where(NextPlan.user_id == user_id))).scalars().all())


def _locked_tables(statement) -> list[str]:
    return sorted({table.name for from_ in statement.get_final_froms() for table in find_tables(from_)})


@pytest.fixture(name="lock_trace")
def lock_trace_fixture(recorder_db):
    events: list[str] = []
    engine = record_usages.engine

    def on_orm(state):
        statement = state.statement
        if state.is_select and statement._for_update_arg is not None:
            events.extend(f"lock {name}" for name in _locked_tables(statement))
        elif state.is_update and getattr(getattr(statement, "table", None), "name", None) == "named_locks":
            events.append("named lock")

    def on_cursor(_conn, _cursor, statement, _parameters, _context, _executemany):
        text = " ".join(statement.split()).lower()
        for verb in ("insert into ", "update ", "delete from "):
            if text.startswith(verb):
                table = text[len(verb) :].split(" ", 1)[0].strip('"`')
                if table in WRITTEN:
                    events.append(f"write {table}")

    def on_commit(_conn):
        events.append("commit")

    def on_rollback(_conn):
        events.append("rollback")

    event.listen(Session, "do_orm_execute", on_orm)
    event.listen(engine.sync_engine, "before_cursor_execute", on_cursor)
    event.listen(engine.sync_engine, "commit", on_commit)
    event.listen(engine.sync_engine, "rollback", on_rollback)
    yield events
    event.remove(Session, "do_orm_execute", on_orm)
    event.remove(engine.sync_engine, "before_cursor_execute", on_cursor)
    event.remove(engine.sync_engine, "commit", on_commit)
    event.remove(engine.sync_engine, "rollback", on_rollback)


def _renewal_transactions(events: list[str]) -> list[list[str]]:
    transactions: list[list[str]] = [[]]
    for entry in events:
        if entry in ("commit", "rollback"):
            transactions.append([])
        else:
            transactions[-1].append(entry)
    return [transaction for transaction in transactions if "write next_plans" in transaction]


@pytest.mark.asyncio
@pytest.mark.parametrize("template", [True, False], ids=["template-plan", "plain-plan"])
@pytest.mark.parametrize("path", PATHS)
async def test_every_next_plan_renewal_takes_the_named_lock_before_any_user_row(
    recorder_db, lock_trace, path, template
):
    renewing, _ = await _seed(recorder_db, path, template=template)
    lock_trace.clear()

    await _renew(recorder_db, path, renewing)

    assert not await _plan_left(recorder_db, renewing), f"{path} did not apply the next plan"
    renewals = _renewal_transactions(lock_trace)
    assert len(renewals) == 1, lock_trace
    renewal = renewals[0]
    assert "named lock" in renewal, f"{path} renewed without the proxy_secrets named lock: {renewal}"
    first_row_work = min(renewal.index(entry) for entry in ROW_WORK if entry in renewal)
    assert renewal.index("named lock") < first_row_work, (
        f"{path} took rows before the proxy_secrets named lock, the order that deadlocks against a delete, a revoke "
        f"or a group change (named lock first): {renewal}"
    )


@pytest.mark.asyncio
async def test_a_deadlock_while_an_admin_applies_the_next_plan_answers_503(monkeypatch, recorder_db):
    renewing, _ = await _seed(recorder_db, "admin_route")

    async def deadlocked(*_args, **_kwargs):
        raise _deadlock()

    monkeypatch.setattr(user_operation, "reset_user_by_next", deadlocked)

    with pytest.raises(HTTPException) as raised:
        await _renew(recorder_db, "admin_route", renewing)

    assert (raised.value.status_code, raised.value.detail) == (503, LOCK_UNAVAILABLE_DETAIL)
    assert await _plan_left(recorder_db, renewing)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["expire_job", "limit_job"])
async def test_a_job_renewal_that_loses_a_deadlock_is_retried(monkeypatch, recorder_db, path):
    first, _ = await _seed(recorder_db, path)
    second, _ = await _seed(recorder_db, path)
    real_reset = review_users.reset_user_by_next
    attempts: list[int] = []

    async def deadlock_once(db, db_user, **kwargs):
        attempts.append(db_user.id)
        if attempts.count(db_user.id) == 1 and db_user.id == first:
            raise _deadlock()
        return await real_reset(db, db_user, **kwargs)

    monkeypatch.setattr(review_users, "reset_user_by_next", deadlock_once)

    if path == "expire_job":
        await status_review.expire_users_under_lock([first, second])
    else:
        await usage_limits.limit_users_under_lock([first, second], usage_limits.LIMITABLE_STATUSES)

    assert attempts.count(first) == 2, attempts
    assert (await _plan_left(recorder_db, first), await _plan_left(recorder_db, second)) == (False, False)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["expire_job", "limit_job"])
async def test_a_job_renewal_that_keeps_deadlocking_is_left_for_the_next_run_without_stopping_the_others(
    monkeypatch, recorder_db, caplog, path
):
    stuck, _ = await _seed(recorder_db, path)
    other, _ = await _seed(recorder_db, path)
    real_reset = review_users.reset_user_by_next
    attempts: list[int] = []

    async def always_deadlocks(db, db_user, **kwargs):
        attempts.append(db_user.id)
        if db_user.id == stuck:
            raise _deadlock()
        return await real_reset(db, db_user, **kwargs)

    monkeypatch.setattr(review_users, "reset_user_by_next", always_deadlocks)

    with caplog.at_level(logging.ERROR):
        if path == "expire_job":
            await status_review.expire_users_under_lock([stuck, other])
        else:
            await usage_limits.limit_users_under_lock([stuck, other], usage_limits.LIMITABLE_STATUSES)

    assert attempts.count(stuck) == status_review.NEXT_PLAN_ATTEMPTS, attempts
    assert (await _plan_left(recorder_db, stuck), await _plan_left(recorder_db, other)) == (True, False)
    async with recorder_db() as session:
        status = (await session.execute(select(User.status).where(User.id == stuck))).scalar_one()
    assert status == UserStatus.active
    assert any(str(stuck) in record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["expire_job", "limit_job"])
async def test_a_job_renewal_whose_lock_wait_times_out_is_left_for_the_next_run_at_once(
    monkeypatch, recorder_db, caplog, path
):
    waiting, _ = await _seed(recorder_db, path)
    other, _ = await _seed(recorder_db, path)
    real_reset = review_users.reset_user_by_next
    attempts: list[int] = []

    async def times_out(db, db_user, **kwargs):
        attempts.append(db_user.id)
        if db_user.id == waiting:
            raise _lock_wait_timeout()
        return await real_reset(db, db_user, **kwargs)

    monkeypatch.setattr(review_users, "reset_user_by_next", times_out)

    with caplog.at_level(logging.ERROR):
        if path == "expire_job":
            await status_review.expire_users_under_lock([waiting, other])
        else:
            await usage_limits.limit_users_under_lock([waiting, other], usage_limits.LIMITABLE_STATUSES)

    assert attempts.count(waiting) == 1, attempts
    assert (await _plan_left(recorder_db, waiting), await _plan_left(recorder_db, other)) == (True, False)
    assert any(str(waiting) in record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR)


async def _delete_unrelated(recorder_db, _renewing: int, bystander: int) -> str:
    async with recorder_db() as session:
        victim = (await session.execute(select(User).where(User.id == bystander))).scalar_one()
        await user_crud.remove_users(session, [victim])
    async with recorder_db() as session:
        left = (await session.execute(select(User.id).where(User.id == bystander))).scalars().all()
    return "" if not left else f"bystander {bystander} still stored"


async def _revoke_same(recorder_db, renewing: int, _bystander: int) -> str:
    async with recorder_db() as session:
        db_user = await get_user_by_id(session, renewing)
        await UserOperation(OperatorType.API)._revoke_user_sub(session, db_user, OWNER)
    async with recorder_db() as session:
        revoked = (await session.execute(select(User.sub_revoked_at).where(User.id == renewing))).scalar_one()
    return "" if revoked is not None else f"revoke of {renewing} not stored"


PARTNERS = {"delete-unrelated-user": _delete_unrelated, "revoke-same-user": _revoke_same}


@server_database_only
@pytest.mark.asyncio
@pytest.mark.parametrize("partner", sorted(PARTNERS))
@pytest.mark.parametrize("path", PATHS)
async def test_a_next_plan_renewal_overlapping_a_named_lock_writer_neither_deadlocks_nor_loses_either(
    monkeypatch, recorder_db, path, partner
):
    renewing, bystander = await _seed(recorder_db, path, with_wireguard=True)
    inside_the_renewal = asyncio.Event()
    proceed = asyncio.Event()
    real_grant = entitled_module.grant_entitled_secrets_after_plan_change

    async def grant_after_the_partner_queued(db, db_user):
        inside_the_renewal.set()
        await proceed.wait()
        return await real_grant(db, db_user)

    monkeypatch.setattr(entitled_module, "grant_entitled_secrets_after_plan_change", grant_after_the_partner_queued)

    renewal = asyncio.create_task(_renew(recorder_db, path, renewing))
    await asyncio.wait_for(inside_the_renewal.wait(), timeout=30)
    other = asyncio.create_task(PARTNERS[partner](recorder_db, renewing, bystander))
    await asyncio.sleep(1.5)
    proceed.set()
    outcomes = await asyncio.wait_for(asyncio.gather(renewal, other, return_exceptions=True), timeout=90)

    failures = [repr(outcome) for outcome in outcomes if isinstance(outcome, BaseException)]
    if isinstance(outcomes[1], str) and outcomes[1]:
        failures.append(outcomes[1])
    if await _plan_left(recorder_db, renewing):
        failures.append(f"next plan of {renewing} still stored")
    assert not failures, f"{path} renewing onto a template overlapped {partner}: {failures}"
