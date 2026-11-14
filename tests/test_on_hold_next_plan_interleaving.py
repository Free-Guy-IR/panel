from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app import notification
from app.db.crud.user import get_user_by_id
from app.db.models import (
    CoreConfig,
    Group,
    NextPlan,
    ProxyInbound,
    User,
    UserStatus,
    UserStatusCreate,
    UserTemplate,
    UserUsageResetLogs,
)
from app.fork.jobs import status_review, usage_limits
from app.jobs import record_usages, review_users
from app.models.core import CoreType
from app.models.settings import General, OnHoldTimeoutAction
from app.operation import OperatorType, user as user_operation
from app.operation.user import UserOperation
from tests.test_recorder_harness import _database_url, recorder_db_fixture, seed_users_and_nodes  # noqa: F401

OWNER = SimpleNamespace(username="owner", is_owner=True, role=None, id=None)
BOUND = 60
CLOCK_SLACK = timedelta(seconds=2)
DATA_LIMIT = 1000
OLD_HOLD_DURATION = 86400
PLAN_DATA_LIMIT = 5 * 1024**3
PLAN_DURATION = 30 * 86400
PLAN_HOLD_TIMEOUT = 7 * 86400
server_database_only = pytest.mark.skipif(
    _database_url().startswith("sqlite"), reason="needs a server database with row locks"
)


@pytest.fixture(name="nodes")
def nodes_fixture(monkeypatch: pytest.MonkeyPatch, recorder_db) -> SimpleNamespace:
    nodes = SimpleNamespace(events=[], status_changes=[], during_removal=[], row_at_removal=[])

    async def review_push(users):
        nodes.events.extend(("push", user.id, user.status) for user in users)

    async def admin_push(db_user):
        nodes.events.append(("push", db_user.id, db_user.status))

    async def remove_before_the_delete(response):
        for action in nodes.during_removal:
            await action(response.id)
        async with recorder_db() as session:
            nodes.row_at_removal.append(
                (await session.execute(select(User.id).where(User.id == response.id))).scalar_one_or_none()
            )
        nodes.events.append(("remove", response.id, None))

    async def remove_after_the_delete(rows):
        nodes.events.extend(("remove deleted", row.id, None) for row in rows)

    async def status_changed(db_user, status):
        nodes.status_changes.append((db_user.id, status))

    async def nothing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(review_users, "sync_users", review_push)
    monkeypatch.setattr(user_operation, "sync_user", admin_push)
    monkeypatch.setattr(review_users, "sync_remove_user_awaited", remove_before_the_delete)
    monkeypatch.setattr(status_review, "sync_remove_users", remove_after_the_delete)
    monkeypatch.setattr(review_users, "_notify_status_change", status_changed)
    monkeypatch.setattr(review_users, "_notify_next_plan", nothing)
    monkeypatch.setattr(review_users, "GetDB", record_usages.GetDB)
    monkeypatch.setattr(status_review, "GetDB", record_usages.GetDB)
    monkeypatch.setattr(status_review, "_postponed_renewals", {}, raising=False)
    monkeypatch.setattr(UserOperation, "generate_subscription_url", staticmethod(AsyncMock(return_value="")))
    for name in ("user_status_change", "user_data_reset_by_next", "remove_user"):
        monkeypatch.setattr(notification, name, AsyncMock())
    return nodes


def _use_action(monkeypatch: pytest.MonkeyPatch, action: OnHoldTimeoutAction) -> None:
    monkeypatch.setattr(
        review_users, "general_settings", AsyncMock(return_value=General(on_hold_timeout_action=action))
    )


async def _seed_on_hold_user_with_an_on_hold_plan(recorder_db, *, came_online: bool) -> tuple[int, int]:
    _, (user_id,), _ = await seed_users_and_nodes(recorder_db, user_count=1, node_count=0)
    tag = uuid4().hex[:8]
    now = datetime.now(UTC)
    used_traffic = 900 if came_online else 0
    values = {
        "status": UserStatus.on_hold,
        "expire": None,
        "on_hold_expire_duration": OLD_HOLD_DURATION,
        "data_limit": DATA_LIMIT,
        "used_traffic": used_traffic,
    }
    if came_online:
        values.update(online_at=now + timedelta(seconds=5), on_hold_timeout=now + timedelta(days=3))
    else:
        values.update(online_at=None, on_hold_timeout=now - timedelta(hours=1))
    async with recorder_db() as session:
        inbound = ProxyInbound(tag=f"ovpn-{tag}")
        session.add(inbound)
        session.add(CoreConfig(name=inbound.tag, type=CoreType.openvpn, config={"instances": [{"tag": inbound.tag}]}))
        await session.flush()
        group = Group(name=f"hold-{tag}", inbounds=[inbound])
        session.add(group)
        await session.flush()
        template = UserTemplate(
            name=f"hold-plan-{tag}",
            username_prefix=None,
            username_suffix=None,
            extra_settings=None,
            groups=[group],
            data_limit=PLAN_DATA_LIMIT,
            expire_duration=PLAN_DURATION,
            on_hold_timeout=PLAN_HOLD_TIMEOUT,
            status=UserStatusCreate.on_hold,
        )
        session.add(template)
        await session.flush()
        session.add(NextPlan(user_id=user_id, user_template_id=template.id))
        await session.execute(update(User).where(User.id == user_id).values(**values))
        await session.commit()
    return user_id, used_traffic


async def _admin_activates_the_next_plan(recorder_db, user_id: int) -> None:
    async with recorder_db() as session:
        db_user = await get_user_by_id(session, user_id)
        await UserOperation(OperatorType.API)._active_next_plan(session, db_user, OWNER)


async def _usage_tick_bills_the_user_past_its_limit(recorder_db, user_id: int) -> None:
    async with recorder_db() as session:
        await session.execute(
            update(User)
            .where(User.id == user_id)
            .values(used_traffic=User.used_traffic + DATA_LIMIT, online_at=datetime.now(UTC))
        )
        await session.commit()
    await usage_limits.enforce_user_limits_now([user_id])


RENEWALS = {
    "admin-active-next": (_admin_activates_the_next_plan, 0),
    "usage-tick-limit": (_usage_tick_bills_the_user_past_its_limit, DATA_LIMIT),
}


def _renew_right_after_the_review_read(
    monkeypatch: pytest.MonkeyPatch, recorder_db, user_id: int, renewal: str, window: list[datetime]
) -> None:
    real_candidates = review_users.get_on_hold_to_active_users
    renew, _ = RENEWALS[renewal]

    async def read_then_the_plan_is_renewed(db):
        stale = await real_candidates(db)
        assert [user.id for user in stale] == [user_id]
        window.append(datetime.now(UTC))
        await renew(recorder_db, user_id)
        window.append(datetime.now(UTC))
        return stale

    monkeypatch.setattr(review_users, "get_on_hold_to_active_users", read_then_the_plan_is_renewed)


async def _stored(recorder_db, user_id: int) -> SimpleNamespace | None:
    async with recorder_db() as session:
        row = (
            await session.execute(
                select(
                    User.status,
                    User.expire,
                    User.online_at,
                    User.edit_at,
                    User.on_hold_timeout,
                    User.on_hold_expire_duration,
                    User.data_limit,
                    User.used_traffic,
                    User.usage_epoch,
                ).where(User.id == user_id)
            )
        ).one_or_none()
        if row is None:
            return None
        resets = select(UserUsageResetLogs.used_traffic_at_reset).where(UserUsageResetLogs.user_id == user_id)
        logged = [int(value) for value in (await session.execute(resets)).scalars()]
        plans = list((await session.execute(select(NextPlan.id).where(NextPlan.user_id == user_id))).scalars())
    status, expire, online_at, edit_at, on_hold_timeout, on_hold_expire_duration, data_limit, used, epoch = row
    return SimpleNamespace(
        status=status,
        expire=expire,
        online_at=online_at,
        edit_at=edit_at,
        on_hold_timeout=on_hold_timeout,
        on_hold_expire_duration=on_hold_expire_duration,
        data_limit=data_limit,
        used_traffic=int(used),
        usage_epoch=int(epoch),
        logged=logged,
        plans=plans,
    )


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _within(value: datetime | None, window: list[datetime]) -> bool:
    value = _aware(value)
    return value is not None and window[0] - CLOCK_SLACK <= value <= window[-1] + CLOCK_SLACK


def _left_by_the_renewal(stored: SimpleNamespace | None, window: list[datetime]) -> dict:
    if stored is None:
        return {"stored": False}
    timeout = _aware(stored.on_hold_timeout)
    return {
        "stored": True,
        "status": stored.status,
        "expire": _aware(stored.expire),
        "online_at": _aware(stored.online_at),
        "edit_at is the renewal time": _within(stored.edit_at, window),
        "on_hold_timeout is the renewal time plus the plan timeout": timeout is not None
        and _within(timeout - timedelta(seconds=PLAN_HOLD_TIMEOUT), window),
        "on_hold_expire_duration": stored.on_hold_expire_duration,
        "data_limit": stored.data_limit,
        "used_traffic": stored.used_traffic,
        "usage_epoch": stored.usage_epoch,
        "traffic logged by resets": stored.logged,
        "next plans left": stored.plans,
    }


def _the_renewed_hold(logged_traffic: int) -> dict:
    return {
        "stored": True,
        "status": UserStatus.on_hold,
        "expire": None,
        "online_at": None,
        "edit_at is the renewal time": True,
        "on_hold_timeout is the renewal time plus the plan timeout": True,
        "on_hold_expire_duration": PLAN_DURATION,
        "data_limit": PLAN_DATA_LIMIT,
        "used_traffic": 0,
        "usage_epoch": 1,
        "traffic logged by resets": [logged_traffic],
        "next plans left": [],
    }


def _events_of(nodes: SimpleNamespace, user_id: int) -> list[tuple]:
    return [event for event in nodes.events if event[1] == user_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("renewal", sorted(RENEWALS))
async def test_a_start_read_before_an_on_hold_renewal_does_not_start_the_renewed_plan(
    monkeypatch: pytest.MonkeyPatch, recorder_db, nodes, renewal
):
    _use_action(monkeypatch, OnHoldTimeoutAction.activate)
    user_id, used_traffic = await _seed_on_hold_user_with_an_on_hold_plan(recorder_db, came_online=True)
    window: list[datetime] = []
    _renew_right_after_the_review_read(monkeypatch, recorder_db, user_id, renewal, window)

    await asyncio.wait_for(review_users.on_hold_to_active_users_job(), BOUND)

    expected = _the_renewed_hold(used_traffic + RENEWALS[renewal][1])
    assert _left_by_the_renewal(await _stored(recorder_db, user_id), window) == expected
    assert _events_of(nodes, user_id) == [("push", user_id, UserStatus.on_hold)]
    assert nodes.status_changes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("renewal", sorted(RENEWALS))
@pytest.mark.parametrize("action", [OnHoldTimeoutAction.disable, OnHoldTimeoutAction.delete])
async def test_a_timeout_read_before_an_on_hold_renewal_neither_disables_nor_deletes_the_renewed_user(
    monkeypatch: pytest.MonkeyPatch, recorder_db, nodes, action, renewal
):
    _use_action(monkeypatch, action)
    user_id, used_traffic = await _seed_on_hold_user_with_an_on_hold_plan(recorder_db, came_online=False)
    window: list[datetime] = []
    _renew_right_after_the_review_read(monkeypatch, recorder_db, user_id, renewal, window)

    await asyncio.wait_for(review_users.on_hold_to_active_users_job(), BOUND)

    expected = _the_renewed_hold(used_traffic + RENEWALS[renewal][1])
    assert _left_by_the_renewal(await _stored(recorder_db, user_id), window) == expected
    assert _events_of(nodes, user_id) == [("push", user_id, UserStatus.on_hold)]
    assert nodes.status_changes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("renewal", sorted(RENEWALS))
async def test_an_on_hold_renewal_during_the_timeout_delete_keeps_the_user_and_puts_it_back_on_the_nodes(
    monkeypatch: pytest.MonkeyPatch, recorder_db, nodes, renewal
):
    _use_action(monkeypatch, OnHoldTimeoutAction.delete)
    user_id, used_traffic = await _seed_on_hold_user_with_an_on_hold_plan(recorder_db, came_online=False)
    renew, billed = RENEWALS[renewal]
    window: list[datetime] = []

    async def renew_before_the_removal_reaches_the_nodes(removed_id: int) -> None:
        window.append(datetime.now(UTC))
        await renew(recorder_db, removed_id)
        window.append(datetime.now(UTC))

    nodes.during_removal.append(renew_before_the_removal_reaches_the_nodes)

    await asyncio.wait_for(review_users.on_hold_to_active_users_job(), BOUND)

    assert _left_by_the_renewal(await _stored(recorder_db, user_id), window) == _the_renewed_hold(used_traffic + billed)
    assert nodes.row_at_removal == [user_id]
    assert _events_of(nodes, user_id) == [
        ("push", user_id, UserStatus.on_hold),
        ("remove", user_id, None),
        ("push", user_id, UserStatus.on_hold),
    ]
    assert nodes.status_changes == []


@server_database_only
@pytest.mark.asyncio
async def test_an_admin_renewal_waiting_behind_the_on_hold_start_leaves_one_consistent_hold(
    monkeypatch: pytest.MonkeyPatch, recorder_db, nodes
):
    _use_action(monkeypatch, OnHoldTimeoutAction.activate)
    user_id, used_traffic = await _seed_on_hold_user_with_an_on_hold_plan(recorder_db, came_online=True)
    start_holds_the_row = asyncio.Event()
    finish_the_start = asyncio.Event()
    renewal_in_refresh = asyncio.Event()
    refresh_trace: list[str] = []
    real_start = status_review.start_users_expire
    real_refresh = user_operation.refresh_user_state_for_update

    async def start_once_the_renewal_waits(db, users):
        start_holds_the_row.set()
        await finish_the_start.wait()
        return await real_start(db, users)

    async def traced_refresh(db, db_user, *args):
        refresh_trace.append("entered")
        renewal_in_refresh.set()
        refreshed = await real_refresh(db, db_user, *args)
        refresh_trace.append("returned")
        return refreshed

    monkeypatch.setattr(status_review, "start_users_expire", start_once_the_renewal_waits)
    monkeypatch.setattr(user_operation, "refresh_user_state_for_update", traced_refresh)

    tasks = [asyncio.create_task(review_users.on_hold_to_active_users_job())]
    window = [datetime.now(UTC)]
    try:
        await asyncio.wait_for(start_holds_the_row.wait(), BOUND)
        window = [datetime.now(UTC)]
        tasks.append(asyncio.create_task(_admin_activates_the_next_plan(recorder_db, user_id)))
        await asyncio.wait_for(renewal_in_refresh.wait(), BOUND)
        await asyncio.sleep(1.0)
        waited_in_refresh = refresh_trace == ["entered"] and not tasks[1].done()
    finally:
        finish_the_start.set()
        outcomes = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), BOUND)
    window.append(datetime.now(UTC))

    assert [repr(outcome) for outcome in outcomes if isinstance(outcome, BaseException)] == []
    assert _left_by_the_renewal(await _stored(recorder_db, user_id), window) == _the_renewed_hold(used_traffic)
    assert _events_of(nodes, user_id) == [
        ("push", user_id, UserStatus.active),
        ("push", user_id, UserStatus.on_hold),
    ]
    assert nodes.status_changes == [(user_id, UserStatus.active)]
    assert waited_in_refresh, refresh_trace
