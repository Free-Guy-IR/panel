from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select, update

import app.settings as settings_module
from app import notification
from app.db.crud import user as user_crud
from app.db.crud.admin import get_admin_by_id
from app.db.models import Admin, User, UserStatus
from app.fork.models.connection import ConnectionRestriction
from app.models.admin import AdminDetails, AdminRoleData
from app.models.user import BulkUsersSetOwner
from app.operation import OperatorType, admin as admin_operation, user as user_operation
from app.operation.admin import AdminOperation
from app.operation.user import UserOperation
from tests.test_recorder_harness import _database_url, recorder_db_fixture, seed_users_and_nodes  # noqa: F401
from tests.test_status_lock_order_interleavings import _deadlocks

pytestmark = pytest.mark.skipif(
    _database_url().startswith("sqlite"), reason="needs a server database with row locks and deadlock detection"
)

OWNER = AdminDetails(id=None, username="owner", role=AdminRoleData(id=1, name="owner", is_owner=True))
HOLD_SECONDS = 0.4
STAGGER_SECONDS = 0.1
BOUND = 120
ACTIONS = {
    "activate_all": ("_activate_all_disabled_users_for_admin", UserStatus.disabled, UserStatus.active),
    "disable_all": ("_disable_all_active_users_for_admin", UserStatus.active, UserStatus.disabled),
}


@pytest.fixture(autouse=True)
async def quiet(monkeypatch: pytest.MonkeyPatch):
    stored = SimpleNamespace(
        telegram={}, webhook={}, notification_settings={}, notification_enable={}, subscription={}, hwid={}, general={}
    )
    monkeypatch.setattr(settings_module, "stored_settings", AsyncMock(return_value=stored))
    await settings_module.refresh_caches()
    monkeypatch.setattr(UserOperation, "generate_subscription_url", staticmethod(AsyncMock(return_value="")))
    for name in ("user_status_change", "modify_user", "reset_user_data_usage"):
        monkeypatch.setattr(notification, name, AsyncMock())
    yield
    await settings_module.refresh_caches()


@pytest.fixture(name="pushes")
def pushes_fixture(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, UserStatus]]:
    pushes: list[tuple[int, UserStatus]] = []

    async def admin_push(users):
        pushes.extend((user.id, user.status) for user in users)

    async def user_push(db_user):
        pushes.append((db_user.id, db_user.status))

    monkeypatch.setattr(admin_operation, "sync_users", admin_push)
    monkeypatch.setattr(user_operation, "sync_user", user_push)
    monkeypatch.setattr(user_operation, "sync_users", admin_push)
    return pushes


def _hold_after(monkeypatch, module, name: str) -> None:
    real = getattr(module, name)

    async def hold(*args, **kwargs):
        result = await real(*args, **kwargs)
        await asyncio.sleep(HOLD_SECONDS)
        return result

    monkeypatch.setattr(module, name, hold)


async def _seed(recorder_db, status: UserStatus) -> SimpleNamespace:
    home, (stay, out_one, out_two), _ = await seed_users_and_nodes(recorder_db, user_count=3, node_count=0)
    away, (in_one, in_two), _ = await seed_users_and_nodes(recorder_db, user_count=2, node_count=0)
    users = (stay, out_one, out_two, in_one, in_two)
    async with recorder_db() as session:
        await session.execute(
            update(User)
            .where(User.id.in_(users))
            .values(status=status, used_traffic=1000, last_status_change=datetime.now(UTC) - timedelta(minutes=40))
        )
        if status == UserStatus.disabled:
            session.add_all(
                ConnectionRestriction(
                    user_id=user_id,
                    created_at=datetime.now(UTC) - timedelta(minutes=30),
                    previous_status=UserStatus.active.value,
                    disable_minutes=30,
                    restore_at=datetime.now(UTC) + timedelta(minutes=30),
                    active=True,
                )
                for user_id in users
            )
        await session.commit()
        names = dict((await session.execute(select(Admin.id, Admin.username).where(Admin.id.in_([home, away])))).all())
    return SimpleNamespace(
        home=home,
        away=away,
        home_name=names[home],
        away_name=names[away],
        stay=stay,
        out_ids=(out_one, out_two),
        in_ids=(in_one, in_two),
        users=users,
    )


async def _final(recorder_db, user_ids) -> dict[int, tuple[int, UserStatus, int]]:
    async with recorder_db() as session:
        rows = (await session.execute(select(User.id, User.admin_id, User.status).where(User.id.in_(user_ids)))).all()
        active = (
            (
                await session.execute(
                    select(ConnectionRestriction.user_id).where(
                        ConnectionRestriction.user_id.in_(user_ids), ConnectionRestriction.active.is_(True)
                    )
                )
            )
            .scalars()
            .all()
        )
    return {user_id: (admin_id, status, active.count(user_id)) for user_id, admin_id, status in rows}


def _mover(recorder_db, mover: str, user_ids: tuple[int, ...], destination: str):
    async def move():
        async with recorder_db() as db:
            operation = UserOperation(OperatorType.API)
            if mover == "set_owner":
                await operation.set_owner_by_id(db, user_ids[0], destination, OWNER)
            else:
                await operation.bulk_set_owner(
                    db, BulkUsersSetOwner(ids=set(user_ids), admin_username=destination), OWNER
                )

    return move


def _admin_wide(recorder_db, admin_id: int, method: str):
    async def run():
        async with recorder_db() as db:
            db_admin = await get_admin_by_id(db, admin_id)
            await getattr(AdminOperation(OperatorType.API), method)(db, db_admin, OWNER)

    return run


async def _run(label: str, step, outcomes: dict[str, str]) -> None:
    try:
        await step()
        outcomes[label] = "committed"
    except HTTPException as exc:
        outcomes[label] = f"answered {exc.status_code}"
    except Exception as exc:
        outcomes[label] = f"aborted: {type(exc).__name__}: {getattr(exc, 'orig', exc)!r}"[:300]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", list(ACTIONS))
async def test_an_admin_wide_change_pushes_a_user_moved_out_after_it_was_changed_but_before_the_push_reread(
    monkeypatch: pytest.MonkeyPatch, recorder_db, pushes, action
):
    method, before, after = ACTIONS[action]
    home, (user_id,), _ = await seed_users_and_nodes(recorder_db, user_count=1, node_count=0)
    away, _, _ = await seed_users_and_nodes(recorder_db, user_count=0, node_count=0)
    async with recorder_db() as session:
        await session.execute(
            update(User)
            .where(User.id == user_id)
            .values(status=before, last_status_change=datetime.now(UTC) - timedelta(minutes=40))
        )
        if before == UserStatus.disabled:
            session.add(
                ConnectionRestriction(
                    user_id=user_id,
                    created_at=datetime.now(UTC) - timedelta(minutes=30),
                    previous_status=UserStatus.active.value,
                    disable_minutes=30,
                    restore_at=datetime.now(UTC) + timedelta(minutes=30),
                    active=True,
                )
            )
        await session.commit()

    real_visible = AdminOperation._visible_users_of

    async def move_out_then_reread(db, db_admin, caller):
        async with recorder_db() as mover:
            await mover.execute(update(User).where(User.id == user_id).values(admin_id=away))
            await mover.commit()
        return await real_visible(db, db_admin, caller)

    monkeypatch.setattr(AdminOperation, "_visible_users_of", staticmethod(move_out_then_reread))

    async with recorder_db() as db:
        db_admin = await get_admin_by_id(db, home)
        await getattr(AdminOperation(OperatorType.API), method)(db, db_admin, OWNER)

    async with recorder_db() as session:
        row = (await session.execute(select(User.admin_id, User.status).where(User.id == user_id))).one()
    assert row.admin_id == away
    assert row.status == after
    assert (user_id, after) in pushes, (
        "the admin-wide change did not push a user it changed and that was moved out before the push re-read",
        pushes,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["admin-wide-first", "mover-first"])
@pytest.mark.parametrize("direction", ["into", "out", "into-and-out"])
@pytest.mark.parametrize("mover", ["set_owner", "bulk_set_owner"])
@pytest.mark.parametrize("action", list(ACTIONS))
async def test_users_moved_into_or_out_of_the_admin_during_an_admin_wide_change_are_changed_whole_or_not_at_all(
    monkeypatch: pytest.MonkeyPatch, recorder_db, pushes, action, mover, direction, first
):
    method, before, after = ACTIONS[action]
    seed = await _seed(recorder_db, before)
    _hold_after(monkeypatch, admin_operation, "close_visible_restrictions")
    _hold_after(monkeypatch, user_crud, "lock_owner_rows")

    moved = {}
    if direction in ("into", "into-and-out"):
        moved["mover-into"] = (_mover(recorder_db, mover, seed.in_ids, seed.home_name), seed.in_ids, seed.home)
    if direction in ("out", "into-and-out"):
        moved["mover-out"] = (_mover(recorder_db, mover, seed.out_ids, seed.away_name), seed.out_ids, seed.away)
    count = 1 if mover == "set_owner" else 2
    steps = {"admin-wide": _admin_wide(recorder_db, seed.home, method)} | {
        label: step for label, (step, _ids, _destination) in moved.items()
    }
    order = ["admin-wide", *moved] if first == "admin-wide-first" else [*moved, "admin-wide"]

    outcomes: dict[str, str] = {}
    deadlocks_before = await _deadlocks()
    tasks = []
    for label in order:
        tasks.append(asyncio.create_task(_run(label, steps[label], outcomes)))
        await asyncio.sleep(STAGGER_SECONDS)
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=BOUND)
    await asyncio.sleep(1)
    deadlocks_after = await _deadlocks()

    assert outcomes == dict.fromkeys(order, "committed"), outcomes
    if deadlocks_before is not None:
        assert deadlocks_after - deadlocks_before == 0, outcomes

    final = await _final(recorder_db, seed.users)
    restricted = before == UserStatus.disabled
    untouched = (before, 1 if restricted else 0)
    changed = (after, 0)
    assert final[seed.stay][1:] == changed, (final, outcomes)
    for _step, ids, destination in moved.values():
        for user_id in ids[:count]:
            admin_id, status, active = final[user_id]
            assert admin_id == destination, (user_id, final, outcomes)
            assert (status, active) in (untouched, changed), (
                "a user moved during the admin-wide change was changed by half of it",
                user_id,
                final,
                outcomes,
            )
        for user_id in ids[count:]:
            assert final[user_id][1:] == (untouched if destination == seed.home else changed), (user_id, final)
    for user_id, (_admin_id, status, _active) in final.items():
        if status != before:
            assert (user_id, status) in pushes, (user_id, status, pushes, outcomes)
