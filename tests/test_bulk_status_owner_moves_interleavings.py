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
from app.db.crud import bulk as bulk_crud, user as user_crud
from app.db.models import Admin, User, UserStatus
from app.models.admin import AdminDetails, AdminRoleData
from app.models.user import BulkUser, BulkUsersSetOwner
from app.operation import OperatorType
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
SHIFT_SECONDS = 7200


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


def _hold_after(monkeypatch, module, name: str) -> None:
    real = getattr(module, name)

    async def hold(*args, **kwargs):
        result = await real(*args, **kwargs)
        await asyncio.sleep(HOLD_SECONDS)
        return result

    monkeypatch.setattr(module, name, hold)


async def _seed(recorder_db) -> SimpleNamespace:
    home, (stay_one, stay_two, stay_three), _ = await seed_users_and_nodes(recorder_db, user_count=3, node_count=0)
    away, (in_one, in_two), _ = await seed_users_and_nodes(recorder_db, user_count=2, node_count=0)
    users = (stay_one, stay_two, stay_three, in_one, in_two)
    async with recorder_db() as session:
        await session.execute(
            update(User)
            .where(User.id.in_(users))
            .values(status=UserStatus.active, expire=datetime.now(UTC) + timedelta(hours=1), used_traffic=1000)
        )
        await session.commit()
        names = dict((await session.execute(select(Admin.id, Admin.username).where(Admin.id.in_([home, away])))).all())
    return SimpleNamespace(
        home=home,
        away=away,
        home_name=names[home],
        stays=(stay_one, stay_two, stay_three),
        in_ids=(in_one, in_two),
        users=users,
    )


def _bulk_expire(recorder_db, admin_id: int):
    async def run():
        async with recorder_db() as db:
            await bulk_crud.update_users_expire(db, BulkUser(admins={admin_id}, amount=-SHIFT_SECONDS))

    return run


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


async def _run(label: str, step, outcomes: dict[str, str]) -> None:
    try:
        await step()
        outcomes[label] = "committed"
    except HTTPException as exc:
        outcomes[label] = f"answered {exc.status_code}"
    except Exception as exc:
        outcomes[label] = f"aborted: {type(exc).__name__}: {getattr(exc, 'orig', exc)!r}"[:300]


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["bulk-first", "mover-first"])
@pytest.mark.parametrize("mover", ["set_owner", "bulk_set_owner"])
async def test_a_bulk_status_change_and_users_moved_into_its_admin_both_commit_without_a_deadlock(
    monkeypatch: pytest.MonkeyPatch, recorder_db, mover, first
):
    seed = await _seed(recorder_db)
    _hold_after(monkeypatch, bulk_crud, "_lock_bulk_targets")
    _hold_after(monkeypatch, user_crud, "lock_owner_rows")

    steps = {
        "bulk": _bulk_expire(recorder_db, seed.home),
        "mover": _mover(recorder_db, mover, seed.in_ids, seed.home_name),
    }
    order = ["bulk", "mover"] if first == "bulk-first" else ["mover", "bulk"]

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

    async with recorder_db() as session:
        rows = dict((await session.execute(select(User.id, User.status).where(User.id.in_(seed.stays)))).all())
        moved = dict((await session.execute(select(User.id, User.admin_id).where(User.id.in_(seed.in_ids)))).all())
    assert rows == dict.fromkeys(seed.stays, UserStatus.expired), (rows, outcomes)
    count = 1 if mover == "set_owner" else 2
    for user_id in seed.in_ids[:count]:
        assert moved[user_id] == seed.home, (moved, outcomes)


@pytest.mark.asyncio
@pytest.mark.parametrize("mover", ["set_owner", "bulk_set_owner"])
async def test_a_user_moved_out_of_the_admin_while_a_bulk_status_change_waits_keeps_its_expiry(
    monkeypatch: pytest.MonkeyPatch, recorder_db, mover
):
    seed = await _seed(recorder_db)
    leaving = seed.stays[1]
    remaining = [user_id for user_id in seed.stays if user_id != leaving]
    async with recorder_db() as session:
        away_name = (await session.execute(select(Admin.username).where(Admin.id == seed.away))).scalar_one()
        expire_before = (await session.execute(select(User.expire).where(User.id == leaving))).scalar_one()
    _hold_after(monkeypatch, user_crud, "lock_owner_rows")

    steps = {
        "mover": _mover(recorder_db, mover, (leaving,), away_name),
        "bulk": _bulk_expire(recorder_db, seed.home),
    }
    outcomes: dict[str, str] = {}
    deadlocks_before = await _deadlocks()
    tasks = []
    for label in ("mover", "bulk"):
        tasks.append(asyncio.create_task(_run(label, steps[label], outcomes)))
        await asyncio.sleep(STAGGER_SECONDS)
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=BOUND)
    await asyncio.sleep(1)
    deadlocks_after = await _deadlocks()

    assert outcomes == {"mover": "committed", "bulk": "committed"}, outcomes
    if deadlocks_before is not None:
        assert deadlocks_after - deadlocks_before == 0, outcomes

    async with recorder_db() as session:
        rows = dict((await session.execute(select(User.id, User.status).where(User.id.in_(remaining)))).all())
        moved = (await session.execute(select(User.admin_id, User.status, User.expire).where(User.id == leaving))).one()
    assert rows == dict.fromkeys(remaining, UserStatus.expired), (rows, outcomes)
    assert moved.admin_id == seed.away, (moved, outcomes)
    assert moved.status == UserStatus.active, (moved, outcomes)
    assert moved.expire == expire_before, (moved, expire_before, outcomes)
