from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.settings as settings_module
from app import notification
from app.db.crud.admin import get_admin_by_id
from app.db.crud.user import get_user_by_id
from app.db.models import User, UserStatus
from app.fork.jobs import connection_limiter
from app.fork.models.connection import ConnectionRestriction
from app.fork.routers import connection_limit as router_module
from app.models.admin import AdminDetails, AdminRoleData
from app.models.settings import ConnectionLimit
from app.models.user import UserStatusToggle
from app.operation import OperatorType, admin as admin_operation, user as user_operation
from app.operation.admin import AdminOperation
from app.operation.user import UserOperation
from tests.test_recorder_harness import _database_url, recorder_db_fixture, seed_users_and_nodes  # noqa: F401
from tests.test_status_lock_order_interleavings import _deadlocks

pytestmark = pytest.mark.skipif(
    _database_url().startswith("sqlite"), reason="needs a server database with row locks and deadlock detection"
)

OWNER = AdminDetails(id=None, username="owner", role=AdminRoleData(id=1, name="owner", is_owner=True))
SETTINGS = ConnectionLimit(
    enabled=True, monitor_only=False, enforcement_enabled=True, persistence_cycles=1, punishment_steps=[30]
)
HOLD_SECONDS = 1.5
BOUND = 120
LOCK_TIMEOUT_SECONDS = 1
RELEASE_LOCK_UNAVAILABLE_DETAIL = (
    "another request held this user or its restriction and the release could not lock them in time; "
    "nothing was changed, retry shortly"
)
FORGET_LOCK_UNAVAILABLE_DETAIL = (
    "another request held some of this user's past restrictions and they could not be cleared in time; "
    "the release itself is kept, retry shortly"
)


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
def pushes_fixture(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int, UserStatus]]:
    pushes: list[tuple[str, int, UserStatus]] = []

    async def release_push(db_user):
        pushes.append(("release", db_user.id, db_user.status))
        return db_user

    async def admin_wide_push(users):
        pushes.extend(("admin-wide", db_user.id, db_user.status) for db_user in users)

    async def toggle_push(db_user):
        pushes.append(("toggle", db_user.id, db_user.status))

    async def limiter_push(users):
        pushes.extend(("limiter", db_user.id, db_user.status) for db_user in users)

    monkeypatch.setattr(router_module.user_operator, "update_user", release_push)
    monkeypatch.setattr(admin_operation, "sync_users", admin_wide_push)
    monkeypatch.setattr(user_operation, "sync_user", toggle_push)
    monkeypatch.setattr(connection_limiter, "_push_to_nodes", limiter_push)
    return pushes


async def _seed(recorder_db, *, due: bool) -> SimpleNamespace:
    admin_id, (user_id, other_id), _ = await seed_users_and_nodes(recorder_db, user_count=2, node_count=0)
    async with recorder_db() as session:
        await session.execute(
            update(User)
            .where(User.id == user_id)
            .values(status=UserStatus.disabled, last_status_change=datetime.now(UTC) - timedelta(minutes=40))
        )
        restriction = ConnectionRestriction(
            user_id=user_id,
            created_at=datetime.now(UTC) - timedelta(minutes=30),
            previous_status=UserStatus.active.value,
            disable_minutes=30,
            restore_at=datetime.now(UTC) + (timedelta(seconds=-1) if due else timedelta(minutes=30)),
            active=True,
        )
        session.add(restriction)
        await session.commit()
        restriction_id = restriction.id
    return SimpleNamespace(admin_id=admin_id, user_id=user_id, other_id=other_id, restriction_id=restriction_id)


async def _final(recorder_db, user_id: int) -> tuple[UserStatus, list[int]]:
    async with recorder_db() as session:
        status = (await session.execute(select(User.status).where(User.id == user_id))).scalar_one()
        active = (
            (
                await session.execute(
                    select(ConnectionRestriction.id).where(
                        ConnectionRestriction.user_id == user_id, ConnectionRestriction.active.is_(True)
                    )
                )
            )
            .scalars()
            .all()
        )
    return status, list(active)


async def _outcome(step) -> str:
    try:
        await step()
        return "committed"
    except HTTPException as exc:
        return f"answered {exc.status_code}"
    except Exception as exc:
        return f"aborted: {type(exc).__name__}: {getattr(exc, 'orig', exc)!r}"[:300]


def _release_holding_its_commit(recorder_db, user_id: int, forget: bool, holding: asyncio.Event):
    async def run():
        async with recorder_db() as db:
            real_commit = db.commit
            held: list[bool] = []

            async def commit_after_the_window():
                if not held:
                    held.append(True)
                    holding.set()
                    await asyncio.sleep(HOLD_SECONDS)
                await real_commit()

            db.commit = commit_after_the_window
            await router_module.release_user(user_id=user_id, forget=forget, db=db, admin=OWNER)

    return run


def _admin_wide_disable(recorder_db, seed):
    async def run():
        async with recorder_db() as db:
            db_admin = await get_admin_by_id(db, seed.admin_id)
            await AdminOperation(OperatorType.API)._disable_all_active_users_for_admin(db, db_admin, OWNER)

    return run


def _toggle_disable(recorder_db, seed):
    async def run():
        async with recorder_db() as db:
            db_user = await get_user_by_id(db, seed.user_id, load_admin_role=True)
            await UserOperation(OperatorType.API)._set_user_disabled(
                db, db_user, UserStatusToggle(disabled=True), OWNER
            )

    return run


def _limiter_release_then_enforce(recorder_db, seed):
    observation = SimpleNamespace(
        user_id=seed.user_id, verdict="over_limit", streak=5, devices=4, limit_applied=2, details={}, reasons=["again"]
    )

    async def run():
        async with recorder_db() as db:
            await connection_limiter._release_due(db, SETTINGS)
        async with recorder_db() as db:
            await connection_limiter._enforce(db, [observation], SETTINGS)

    return run


SCENARIOS = {
    "admin-wide-disable": (_admin_wide_disable, False, (UserStatus.disabled, 0)),
    "admin-toggle-disable": (_toggle_disable, False, (UserStatus.disabled, 0)),
    "limiter-release-then-enforce": (_limiter_release_then_enforce, True, (UserStatus.disabled, 1)),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("forget", [False, True], ids=["keep-history", "forget-history"])
@pytest.mark.parametrize("scenario", list(SCENARIOS))
async def test_a_manual_release_never_overwrites_a_status_decided_while_it_was_deciding(
    recorder_db, pushes, scenario, forget
):
    make_competitor, due, expected = SCENARIOS[scenario]
    seed = await _seed(recorder_db, due=due)
    holding = asyncio.Event()
    deadlocks_before = await _deadlocks()

    release_task = asyncio.create_task(
        _outcome(_release_holding_its_commit(recorder_db, seed.user_id, forget, holding))
    )
    await asyncio.wait_for(holding.wait(), timeout=30)
    competitor_task = asyncio.create_task(_outcome(make_competitor(recorder_db, seed)))
    released, competed = await asyncio.wait_for(asyncio.gather(release_task, competitor_task), timeout=BOUND)
    await asyncio.sleep(1)
    deadlocks_after = await _deadlocks()

    assert (released, competed) == ("committed", "committed")
    if deadlocks_before is not None:
        assert deadlocks_after - deadlocks_before == 0
    status, active = await _final(recorder_db, seed.user_id)
    assert (status, len(active)) == expected, (
        "the manual release wrote a status decided from rows another writer changed before it committed",
        scenario,
        status,
        active,
        pushes,
    )
    assert not (active and status != UserStatus.disabled), (status, active)
    told = [pushed for _source, pushed_id, pushed in pushes if pushed_id == seed.user_id]
    assert told and told[-1] == status, (status, told, pushes)


def _lock_timeout_statement(url: str) -> str:
    if url.startswith("postgresql"):
        return f"SET lock_timeout = '{LOCK_TIMEOUT_SECONDS}s'"
    return f"SET SESSION innodb_lock_wait_timeout = {LOCK_TIMEOUT_SECONDS}"


@pytest.fixture(name="impatient_db")
async def impatient_db_fixture(recorder_db):
    url = _database_url()
    engine = create_async_engine(url, poolclass=NullPool)
    statement = _lock_timeout_statement(url)

    @event.listens_for(engine.sync_engine, "connect")
    def _give_up_waiting_quickly(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute(statement)
        cursor.close()

    yield async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    await engine.dispose()


async def _answer(step) -> tuple[int | None, str]:
    try:
        await step()
    except HTTPException as exc:
        return exc.status_code, str(exc.detail)
    except Exception as exc:
        return None, f"{type(exc).__name__}: {getattr(exc, 'orig', exc)!r}"[:300]
    return 204, ""


async def _inactive_ids(recorder_db, user_id: int) -> list[int]:
    async with recorder_db() as session:
        return sorted(
            (
                await session.execute(
                    select(ConnectionRestriction.id).where(
                        ConnectionRestriction.user_id == user_id, ConnectionRestriction.active.is_(False)
                    )
                )
            )
            .scalars()
            .all()
        )


@pytest.mark.asyncio
async def test_a_release_that_cannot_lock_the_user_answers_503_and_changes_nothing(recorder_db, impatient_db, pushes):
    seed = await _seed(recorder_db, due=False)

    async with recorder_db() as holder:
        await holder.execute(select(User.id).where(User.id == seed.user_id).with_for_update())
        async with impatient_db() as db:
            answer = await asyncio.wait_for(
                _answer(lambda: router_module.release_user(user_id=seed.user_id, forget=False, db=db, admin=OWNER)),
                BOUND,
            )
        await holder.rollback()

    assert answer == (503, RELEASE_LOCK_UNAVAILABLE_DETAIL)
    assert await _final(recorder_db, seed.user_id) == (UserStatus.disabled, [seed.restriction_id])
    assert pushes == []


@pytest.mark.asyncio
async def test_a_forget_that_cannot_lock_the_history_answers_503_and_keeps_the_release(
    recorder_db, impatient_db, pushes
):
    seed = await _seed(recorder_db, due=False)
    async with recorder_db() as session:
        earlier = ConnectionRestriction(
            user_id=seed.user_id,
            created_at=datetime.now(UTC) - timedelta(days=2),
            previous_status=UserStatus.active.value,
            disable_minutes=30,
            restored_at=datetime.now(UTC) - timedelta(days=2) + timedelta(minutes=30),
            active=False,
        )
        session.add(earlier)
        await session.commit()
        earlier_id = earlier.id

    async with recorder_db() as holder:
        await holder.execute(
            select(ConnectionRestriction.id).where(ConnectionRestriction.id == earlier_id).with_for_update()
        )
        async with impatient_db() as db:
            answer = await asyncio.wait_for(
                _answer(lambda: router_module.release_user(user_id=seed.user_id, forget=True, db=db, admin=OWNER)),
                BOUND,
            )
        await holder.rollback()

    assert answer == (503, FORGET_LOCK_UNAVAILABLE_DETAIL)
    assert await _final(recorder_db, seed.user_id) == (UserStatus.active, [])
    assert await _inactive_ids(recorder_db, seed.user_id) == sorted([seed.restriction_id, earlier_id])
    assert pushes == [("release", seed.user_id, UserStatus.active)]
