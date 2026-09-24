from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.db.models import UserStatus
from app.jobs import review_users
from app.models.proxy import ProxyTable
from app.models.settings import General, OnHoldTimeoutAction
from app.node import sync as node_sync


def _db_user(user_id: int, *, next_plan=None) -> SimpleNamespace:
    return SimpleNamespace(id=user_id, username=f"hold{user_id}", next_plan=next_plan)


def _get_db(session):
    @asynccontextmanager
    async def _ctx():
        yield session

    return _ctx


async def _validate(db_user, include_subscription_url=True):
    return SimpleNamespace(id=db_user.id, username=db_user.username)


async def _run_job(action: OnHoldTimeoutAction, users):
    session = AsyncMock()
    patches = {
        "GetDB": patch("app.jobs.review_users.GetDB", _get_db(session)),
        "get_users": patch(
            "app.jobs.review_users.get_on_hold_to_active_users", new_callable=AsyncMock, return_value=users
        ),
        "settings": patch(
            "app.jobs.review_users.general_settings",
            new_callable=AsyncMock,
            return_value=General(on_hold_timeout_action=action),
        ),
        "activate": patch("app.jobs.review_users.start_on_hold_users_under_lock", new_callable=AsyncMock),
        "disable": patch("app.jobs.review_users.disable_on_hold_users_under_lock", new_callable=AsyncMock),
        "delete": patch("app.jobs.review_users.remove_on_hold_users_under_lock", new_callable=AsyncMock),
    }
    mocks = {name: p.start() for name, p in patches.items()}
    try:
        await review_users.on_hold_to_active_users_job()
    finally:
        for p in patches.values():
            p.stop()
    return mocks


@pytest.mark.parametrize(
    ("action", "chosen"),
    [
        (OnHoldTimeoutAction.activate, "activate"),
        (OnHoldTimeoutAction.disable, "disable"),
        (OnHoldTimeoutAction.delete, "delete"),
    ],
)
@pytest.mark.asyncio
async def test_each_action_hands_the_candidate_ids_to_its_locked_path(action, chosen):
    users = [_db_user(2), _db_user(1)]
    mocks = await _run_job(action, users)

    mocks[chosen].assert_awaited_once_with([2, 1])
    for other in {"activate", "disable", "delete"} - {chosen}:
        mocks[other].assert_not_called()


@pytest.mark.asyncio
async def test_no_candidates_reads_no_settings_and_takes_no_path():
    mocks = await _run_job(OnHoldTimeoutAction.delete, [])

    mocks["settings"].assert_not_called()
    for name in ("activate", "disable", "delete"):
        mocks[name].assert_not_called()


@pytest.mark.asyncio
async def test_delete_action_removes_from_nodes_before_database():
    users = [_db_user(1), _db_user(2)]
    calls: list[str] = []

    async def _remove_awaited(user):
        calls.append(f"node:{user.id}")

    async def _remove_users(_db, db_users):
        calls.append("db:" + ",".join(str(u.id) for u in db_users))

    with (
        patch("app.jobs.review_users.sync_remove_user_awaited", side_effect=_remove_awaited),
        patch("app.jobs.review_users.remove_users", side_effect=_remove_users),
        patch("app.jobs.review_users.notification.remove_user", new_callable=AsyncMock),
        patch.object(review_users.user_operator, "validate_user", side_effect=_validate),
    ):
        await review_users.remove_on_hold_users(AsyncMock(), users)

    assert calls == ["node:1", "node:2", "db:1,2"]


@pytest.mark.asyncio
async def test_delete_action_rows_untouched_after_node_failure():
    users = [_db_user(3)]
    removed: list[str] = []

    async def _remove_users(_db, db_users):
        removed.extend(str(u.id) for u in db_users)

    with (
        patch("app.jobs.review_users.remove_users", side_effect=_remove_users),
        patch(
            "app.jobs.review_users.sync_remove_user_awaited",
            new_callable=AsyncMock,
            side_effect=RuntimeError("node down"),
        ),
        patch.object(review_users.user_operator, "validate_user", side_effect=_validate),
        pytest.raises(RuntimeError),
    ):
        await review_users.remove_on_hold_users(AsyncMock(), users)

    assert removed == []


@pytest.mark.parametrize("status", [UserStatus.disabled, UserStatus.active])
@pytest.mark.asyncio
async def test_status_changes_outside_expired_and_limited_do_not_consume_next_plan(status):
    planned = _db_user(7, next_plan=SimpleNamespace(data_limit=1))

    with (
        patch("app.jobs.review_users.update_users_status", new_callable=AsyncMock),
        patch("app.jobs.review_users.sync_users", new_callable=AsyncMock) as sync,
        patch.object(review_users.user_operator, "validate_user", side_effect=_validate),
        patch("app.jobs.review_users.notification.user_status_change", new_callable=AsyncMock),
        patch("app.jobs.review_users.reset_user_by_next", new_callable=AsyncMock) as reset_next,
    ):
        await review_users.apply_status_changes(AsyncMock(), [planned], status)

    reset_next.assert_not_called()
    sync.assert_awaited_once_with([planned])


@pytest.mark.asyncio
async def test_awaited_node_removal_serializes_like_fire_and_forget_removal():
    user = SimpleNamespace(id=11, proxy_settings=ProxyTable(), hwid_limit=3)
    dispatched: list = []

    async def _dispatch(proto_user):
        dispatched.append(proto_user)

    with patch("app.node.sync._dispatch_user_update", side_effect=_dispatch):
        await node_sync.remove_user_awaited(user)
        expected = node_sync._serialize_user_for_node(user.id, user.proxy_settings.dict(), hwid_limit=user.hwid_limit)

    assert dispatched == [expected]
