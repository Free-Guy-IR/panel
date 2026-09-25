from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from app import notification
from app.db.crud.admin import get_admin_by_id
from app.db.models import User, UserStatus
from app.fork.jobs import status_review
from app.jobs import record_usages, review_users
from app.models.admin import AdminDetails, AdminRoleData
from app.models.settings import General
from app.models.user import UserStatusToggle
from app.operation import OperatorType, admin as admin_operation, user as user_operation
from app.operation.admin import AdminOperation
from app.operation.user import UserOperation
from tests.test_recorder_harness import CountingNode, recorder_db_fixture, seed_users_and_nodes  # noqa: F401

OWNER = AdminDetails(id=None, username="owner", role=AdminRoleData(id=1, name="owner", is_owner=True))
BOUND = 30


@pytest.fixture(name="pushes")
def pushes_fixture(monkeypatch: pytest.MonkeyPatch, recorder_db) -> list[tuple[str, int, UserStatus]]:
    pushes: list[tuple[str, int, UserStatus]] = []

    def recorder(source: str):
        async def record(users):
            pushes.extend((source, user.id, user.status) for user in users)

        return record

    async def record_one(db_user):
        pushes.append(("user", db_user.id, db_user.status))

    async def validated(db_user, include_subscription_url=True):
        return SimpleNamespace(id=db_user.id, username=db_user.username, status=db_user.status)

    async def nothing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(review_users, "sync_users", recorder("review"))
    monkeypatch.setattr(admin_operation, "sync_users", recorder("admin"))
    monkeypatch.setattr(user_operation, "sync_user", record_one)
    monkeypatch.setattr(review_users, "_notify_status_change", nothing)
    monkeypatch.setattr(review_users, "_notify_next_plan", nothing)
    monkeypatch.setattr(UserOperation, "validate_user", staticmethod(validated))
    monkeypatch.setattr(notification, "user_status_change", AsyncMock())
    monkeypatch.setattr(review_users, "GetDB", record_usages.GetDB)
    monkeypatch.setattr(status_review, "GetDB", record_usages.GetDB)
    monkeypatch.setattr(review_users, "general_settings", AsyncMock(return_value=General()))
    return pushes


def _last_push(pushes, user_id: int) -> UserStatus | None:
    mine = [status for _source, pushed_id, status in pushes if pushed_id == user_id]
    return mine[-1] if mine else None


async def _status(recorder_db, user_id: int) -> UserStatus:
    async with recorder_db() as session:
        return (await session.execute(select(User.status).where(User.id == user_id))).scalar_one()


async def _set(recorder_db, user_id: int, **values) -> None:
    async with recorder_db() as session:
        await session.execute(update(User).where(User.id == user_id).values(**values))
        await session.commit()


async def _admin_wide(recorder_db, admin_id: int, action: str) -> None:
    async with recorder_db() as db:
        db_admin = await get_admin_by_id(db, admin_id)
        await asyncio.wait_for(getattr(AdminOperation(OperatorType.API), action)(db, db_admin, OWNER), BOUND)


async def _single_user_toggle(recorder_db, user_id: int, disabled: bool) -> None:
    async with recorder_db() as db:
        db_user = (await db.execute(select(User).where(User.id == user_id))).unique().scalar_one()
        await asyncio.wait_for(
            UserOperation(OperatorType.API)._set_user_disabled(db, db_user, UserStatusToggle(disabled=disabled), OWNER),
            BOUND,
        )


def _candidate_state(job: str) -> dict:
    now = datetime.now(UTC)
    return {
        "expire": {"status": UserStatus.active, "expire": now - timedelta(minutes=5)},
        "limit": {"status": UserStatus.active, "data_limit": 100, "used_traffic": 200},
        "on_hold": {
            "status": UserStatus.on_hold,
            "expire": None,
            "on_hold_expire_duration": 86400,
            "online_at": now + timedelta(seconds=5),
        },
    }[job]


JOBS = {
    "expire": ("get_active_to_expire_users", review_users.expire_users_job),
    "limit": ("get_active_to_limited_users", review_users.limit_users_job),
    "on_hold": ("get_on_hold_to_active_users", review_users.on_hold_to_active_users_job),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("job", sorted(JOBS))
async def test_a_review_job_that_read_before_an_admin_wide_disable_leaves_the_user_disabled(
    monkeypatch: pytest.MonkeyPatch, recorder_db, pushes, job
):
    admin_id, (user_id,), _ = await seed_users_and_nodes(recorder_db, user_count=1, node_count=0)
    await _set(recorder_db, user_id, **_candidate_state(job))
    candidates_name, run_job = JOBS[job]
    real_candidates = getattr(review_users, candidates_name)

    async def read_then_admin_disables_every_user(db):
        stale = await real_candidates(db)
        assert [user.id for user in stale] == [user_id]
        await _admin_wide(recorder_db, admin_id, "_disable_all_active_users_for_admin")
        return stale

    monkeypatch.setattr(review_users, candidates_name, read_then_admin_disables_every_user)

    await asyncio.wait_for(run_job(), BOUND)

    assert await _status(recorder_db, user_id) == UserStatus.disabled
    assert _last_push(pushes, user_id) == UserStatus.disabled, pushes


@pytest.mark.asyncio
async def test_a_single_user_enable_inside_an_admin_wide_disable_is_the_state_pushed_last(
    monkeypatch: pytest.MonkeyPatch, recorder_db, pushes
):
    admin_id, (user_id, other_id), _ = await seed_users_and_nodes(recorder_db, user_count=2, node_count=0)
    real_visible = AdminOperation._visible_users_of

    async def enable_then_reread(db, db_admin, caller):
        await _single_user_toggle(recorder_db, user_id, disabled=False)
        return await real_visible(db, db_admin, caller)

    monkeypatch.setattr(AdminOperation, "_visible_users_of", staticmethod(enable_then_reread))

    await _admin_wide(recorder_db, admin_id, "_disable_all_active_users_for_admin")

    assert await _status(recorder_db, user_id) == UserStatus.active
    assert await _status(recorder_db, other_id) == UserStatus.disabled
    assert _last_push(pushes, user_id) == UserStatus.active, pushes
    assert _last_push(pushes, other_id) == UserStatus.disabled, pushes


@pytest.mark.asyncio
async def test_an_admin_wide_disable_inside_a_single_user_enable_is_overridden_by_the_later_enable(
    monkeypatch: pytest.MonkeyPatch, recorder_db, pushes
):
    admin_id, (user_id,), _ = await seed_users_and_nodes(recorder_db, user_count=1, node_count=0)
    real_close = user_operation.close_active_restriction

    async def admin_disables_every_user_then_close(db, closed_id, reason):
        await _admin_wide(recorder_db, admin_id, "_disable_all_active_users_for_admin")
        assert await _status(recorder_db, user_id) == UserStatus.disabled
        return await real_close(db, closed_id, reason)

    monkeypatch.setattr(user_operation, "close_active_restriction", admin_disables_every_user_then_close)

    await _single_user_toggle(recorder_db, user_id, disabled=False)

    assert await _status(recorder_db, user_id) == UserStatus.active
    assert _last_push(pushes, user_id) == UserStatus.active, pushes


def _one_node_billing(monkeypatch: pytest.MonkeyPatch, node_id: int, user_id: int, value: int) -> CountingNode:
    node = CountingNode(node_id, {user_id: value})
    monkeypatch.setattr(record_usages.node_manager, "get_healthy_nodes", AsyncMock(return_value=[(node.node_id, node)]))
    return node


@pytest.mark.asyncio
async def test_an_admin_wide_activate_inside_a_usage_tick_ends_limited_when_the_tick_crosses_the_limit(
    monkeypatch: pytest.MonkeyPatch, recorder_db, pushes
):
    admin_id, (user_id,), (node_id,) = await seed_users_and_nodes(recorder_db, user_count=1, node_count=1)
    await _set(recorder_db, user_id, status=UserStatus.disabled, data_limit=1000, used_traffic=900)
    _one_node_billing(monkeypatch, node_id, user_id, 200)
    real_context = record_usages.load_user_usage_context

    async def context_then_admin_activates(uids):
        context = await real_context(uids)
        await _admin_wide(recorder_db, admin_id, "_activate_all_disabled_users_for_admin")
        return context

    monkeypatch.setattr(record_usages, "load_user_usage_context", context_then_admin_activates)

    await asyncio.wait_for(record_usages._record_user_usages_impl(), BOUND)

    assert await _status(recorder_db, user_id) == UserStatus.limited
    assert _last_push(pushes, user_id) == UserStatus.limited, pushes


@pytest.mark.asyncio
async def test_a_usage_tick_inside_an_admin_wide_activate_ends_limited_when_it_crosses_the_limit(
    monkeypatch: pytest.MonkeyPatch, recorder_db, pushes
):
    admin_id, (user_id,), (node_id,) = await seed_users_and_nodes(recorder_db, user_count=1, node_count=1)
    await _set(recorder_db, user_id, status=UserStatus.disabled, data_limit=1000, used_traffic=900)
    _one_node_billing(monkeypatch, node_id, user_id, 200)
    real_visible = AdminOperation._visible_users_of

    async def tick_then_reread(db, db_admin, caller):
        await asyncio.wait_for(record_usages._record_user_usages_impl(), BOUND)
        return await real_visible(db, db_admin, caller)

    monkeypatch.setattr(AdminOperation, "_visible_users_of", staticmethod(tick_then_reread))

    await _admin_wide(recorder_db, admin_id, "_activate_all_disabled_users_for_admin")

    assert await _status(recorder_db, user_id) == UserStatus.limited
    assert _last_push(pushes, user_id) == UserStatus.limited, pushes


@pytest.mark.asyncio
async def test_an_admin_wide_activate_after_a_tick_billed_a_disabled_user_over_its_limit_limits_it(
    monkeypatch: pytest.MonkeyPatch, recorder_db, pushes
):
    admin_id, (user_id,), (node_id,) = await seed_users_and_nodes(recorder_db, user_count=1, node_count=1)
    await _set(recorder_db, user_id, status=UserStatus.disabled, data_limit=1000, used_traffic=900)
    _one_node_billing(monkeypatch, node_id, user_id, 200)

    await asyncio.wait_for(record_usages._record_user_usages_impl(), BOUND)
    assert await _status(recorder_db, user_id) == UserStatus.disabled
    await _admin_wide(recorder_db, admin_id, "_activate_all_disabled_users_for_admin")

    assert await _status(recorder_db, user_id) == UserStatus.limited
    assert _last_push(pushes, user_id) == UserStatus.limited, pushes


async def _give_inbound(recorder_db, user_ids: list[int]) -> None:
    from uuid import uuid4

    from app.db.models import Group, ProxyInbound, users_groups_association

    tag = f"merge-delivery-{uuid4().hex[:8]}"
    async with recorder_db() as session:
        inbound = ProxyInbound(tag=tag)
        group = Group(name=tag[:64], inbounds=[inbound])
        session.add_all([inbound, group])
        await session.flush()
        await session.execute(
            users_groups_association.insert(), [{"user_id": user_id, "groups_id": group.id} for user_id in user_ids]
        )
        await session.commit()


@pytest.fixture(name="deliver")
def deliver_fixture(monkeypatch: pytest.MonkeyPatch, recorder_db):
    from app.node import sync as node_sync

    monkeypatch.setattr(node_sync, "GetDB", record_usages.GetDB)
    monkeypatch.setattr(node_sync, "_current_state", node_sync.CurrentStateReader(query_timeout=BOUND))

    async def deliver(user_id: int) -> list[str]:
        (payload,) = await asyncio.wait_for(node_sync.refresh_node_users("node-1", [str(user_id)]), BOUND)
        assert payload.email == str(user_id)
        return list(payload.inbounds)

    return deliver


@pytest.mark.asyncio
@pytest.mark.parametrize("job", sorted(JOBS))
async def test_a_delivery_of_a_review_job_push_sends_the_admin_wide_disable_that_committed_after_it(
    monkeypatch: pytest.MonkeyPatch, recorder_db, pushes, deliver, job
):
    admin_id, (user_id,), _ = await seed_users_and_nodes(recorder_db, user_count=1, node_count=0)
    await _give_inbound(recorder_db, [user_id])
    await _set(recorder_db, user_id, **_candidate_state(job))
    candidates_name, run_job = JOBS[job]
    real_candidates = getattr(review_users, candidates_name)

    async def read_then_admin_disables_every_user(db):
        stale = await real_candidates(db)
        await _admin_wide(recorder_db, admin_id, "_disable_all_active_users_for_admin")
        return stale

    monkeypatch.setattr(review_users, candidates_name, read_then_admin_disables_every_user)
    await asyncio.wait_for(run_job(), BOUND)

    assert await _status(recorder_db, user_id) == UserStatus.disabled
    assert await deliver(user_id) == []


@pytest.mark.asyncio
async def test_deliveries_of_pushes_queued_in_any_order_all_send_the_state_committed_last(
    monkeypatch: pytest.MonkeyPatch, recorder_db, pushes, deliver
):
    admin_id, (user_id, other_id), _ = await seed_users_and_nodes(recorder_db, user_count=2, node_count=0)
    await _give_inbound(recorder_db, [user_id, other_id])
    real_visible = AdminOperation._visible_users_of

    async def enable_then_reread(db, db_admin, caller):
        await _single_user_toggle(recorder_db, user_id, disabled=False)
        return await real_visible(db, db_admin, caller)

    monkeypatch.setattr(AdminOperation, "_visible_users_of", staticmethod(enable_then_reread))
    await _admin_wide(recorder_db, admin_id, "_disable_all_active_users_for_admin")

    queued = [pushed_id for _source, pushed_id, _status in pushes]
    assert queued.count(user_id) >= 1 and queued.count(other_id) >= 1
    for pushed_id in reversed(queued):
        expected = [] if pushed_id == other_id else None
        delivered = await deliver(pushed_id)
        if expected is None:
            assert delivered, (pushed_id, delivered)
        else:
            assert delivered == expected, (pushed_id, delivered)


@pytest.mark.asyncio
async def test_a_delivery_after_an_admin_wide_activate_races_a_tick_over_the_limit_removes_the_user(
    monkeypatch: pytest.MonkeyPatch, recorder_db, pushes, deliver
):
    admin_id, (user_id,), (node_id,) = await seed_users_and_nodes(recorder_db, user_count=1, node_count=1)
    await _give_inbound(recorder_db, [user_id])
    await _set(recorder_db, user_id, status=UserStatus.disabled, data_limit=1000, used_traffic=900)
    _one_node_billing(monkeypatch, node_id, user_id, 200)
    real_context = record_usages.load_user_usage_context

    async def context_then_admin_activates(uids):
        context = await real_context(uids)
        await _admin_wide(recorder_db, admin_id, "_activate_all_disabled_users_for_admin")
        assert await deliver(user_id)
        return context

    monkeypatch.setattr(record_usages, "load_user_usage_context", context_then_admin_activates)
    await asyncio.wait_for(record_usages._record_user_usages_impl(), BOUND)

    assert await _status(recorder_db, user_id) == UserStatus.limited
    assert await deliver(user_id) == []
