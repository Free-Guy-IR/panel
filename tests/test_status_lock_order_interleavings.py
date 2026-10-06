from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

import app.settings as settings_module
from app import notification
from app.db.crud.admin import get_admin_by_id
from app.db.crud.user import get_user_by_id
from app.db.models import User, UserStatus, UserTemplate, UserUsageResetLogs
from app.fork.jobs import connection_limiter
from app.fork.models.connection import ConnectionRestriction
from app.models.admin import AdminDetails, AdminRoleData
from app.models.settings import ConnectionLimit
from app.models.user import ModifyUserByTemplate, UserStatusToggle
from app.operation import OperatorType, admin as admin_operation, user as user_operation
from app.operation.admin import AdminOperation
from app.operation.user import UserOperation
from app.utils import connection_enforcement
from tests.test_recorder_harness import _database_url, recorder_db_fixture, seed_users_and_nodes  # noqa: F401

pytestmark = pytest.mark.skipif(
    _database_url().startswith("sqlite"), reason="needs a server database with row locks and deadlock detection"
)

OWNER = AdminDetails(id=None, username="owner", role=AdminRoleData(id=1, name="owner", is_owner=True))
SETTINGS = ConnectionLimit(
    enabled=True, monitor_only=False, enforcement_enabled=True, persistence_cycles=1, punishment_steps=[30]
)
TEMPLATE_DATA_LIMIT = 10**9
HOLD_SECONDS = 0.4
STAGGER_SECONDS = 0.1
BOUND = 120
OPS = ("release", "activate_all", "disable_all", "template", "toggle", "enforce")
ORDERS = [OPS[start:] + OPS[:start] for start in range(len(OPS))] + [
    tuple(reversed(OPS[start:] + OPS[:start])) for start in range(0, len(OPS), 2)
]


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

    async def limiter_push(users):
        pushes.extend(("limiter", user.id, user.status) for user in users)

    async def admin_push(users):
        pushes.extend(("admin-wide", user.id, user.status) for user in users)

    async def user_push(db_user):
        pushes.append(("user", db_user.id, db_user.status))

    monkeypatch.setattr(connection_limiter, "_push_to_nodes", limiter_push)
    monkeypatch.setattr(admin_operation, "sync_users", admin_push)
    monkeypatch.setattr(user_operation, "sync_user", user_push)
    return pushes


async def _deadlocks() -> int | None:
    url = _database_url()
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            if url.startswith("postgresql"):
                return int(
                    (
                        await conn.execute(
                            text("SELECT deadlocks FROM pg_stat_database WHERE datname = current_database()")
                        )
                    ).scalar_one()
                )
            if url.startswith("mysql"):
                await conn.execute(text("SET GLOBAL innodb_monitor_enable = 'lock_deadlocks'"))
                return int(
                    (
                        await conn.execute(
                            text("SELECT `COUNT` FROM information_schema.INNODB_METRICS WHERE NAME = 'lock_deadlocks'")
                        )
                    ).scalar_one()
                )
            return None
    finally:
        await engine.dispose()


async def _seed(recorder_db) -> SimpleNamespace:
    admin_id, (renewed, toggled, enforced, restricted, bystander), _ = await seed_users_and_nodes(
        recorder_db, user_count=5, node_count=0
    )
    async with recorder_db() as session:
        await session.execute(
            update(User)
            .where(User.id.in_([renewed, toggled, restricted]))
            .values(status=UserStatus.disabled, used_traffic=1234, data_limit=5000)
        )
        session.add_all(
            ConnectionRestriction(
                user_id=user_id,
                previous_status=UserStatus.active.value,
                disable_minutes=30,
                restore_at=datetime.now(UTC) - timedelta(seconds=1),
                active=True,
            )
            for user_id in (renewed, toggled, restricted)
        )
        template = UserTemplate(
            name=f"chaos-{uuid4().hex[:10]}",
            username_prefix=None,
            username_suffix=None,
            extra_settings=None,
            groups=[],
            data_limit=TEMPLATE_DATA_LIMIT,
            expire_duration=86400,
            reset_usages=True,
        )
        session.add(template)
        await session.commit()
        template_id = template.id
    return SimpleNamespace(
        admin_id=admin_id,
        renewed=renewed,
        toggled=toggled,
        enforced=enforced,
        restricted=restricted,
        bystander=bystander,
        template_id=template_id,
        users=(renewed, toggled, enforced, restricted, bystander),
    )


def _hold_after(monkeypatch, module, name: str) -> None:
    real = getattr(module, name)

    async def hold(*args, **kwargs):
        result = await real(*args, **kwargs)
        await asyncio.sleep(HOLD_SECONDS)
        return result

    monkeypatch.setattr(module, name, hold)


async def _statuses(recorder_db, user_ids) -> dict[int, UserStatus]:
    async with recorder_db() as session:
        rows = await session.execute(select(User.id, User.status).where(User.id.in_(list(user_ids))))
        return dict(rows.all())


def _steps(recorder_db, seed) -> dict:
    async def release():
        async with recorder_db() as db:
            await connection_limiter._release_due(db, SETTINGS)

    def admin_wide(action: str):
        async def run():
            async with recorder_db() as db:
                db_admin = await get_admin_by_id(db, seed.admin_id)
                await getattr(AdminOperation(OperatorType.API), action)(db, db_admin, OWNER)

        return run

    async def template():
        async with recorder_db() as db:
            db_user = await get_user_by_id(db, seed.renewed, load_admin_role=True)
            await UserOperation(OperatorType.API)._modify_user_with_template(
                db, db_user, ModifyUserByTemplate(user_template_id=seed.template_id), OWNER
            )

    async def toggle():
        async with recorder_db() as db:
            db_user = await get_user_by_id(db, seed.toggled, load_admin_role=True)
            await UserOperation(OperatorType.API)._set_user_disabled(
                db, db_user, UserStatusToggle(disabled=False), OWNER
            )

    async def enforce():
        observation = SimpleNamespace(
            user_id=seed.enforced,
            verdict="over_limit",
            streak=5,
            devices=4,
            limit_applied=2,
            details={},
            reasons=["interleaving"],
        )
        async with recorder_db() as db:
            await connection_limiter._enforce(db, [observation], SETTINGS)

    return {
        "release": release,
        "activate_all": admin_wide("_activate_all_disabled_users_for_admin"),
        "disable_all": admin_wide("_disable_all_active_users_for_admin"),
        "template": template,
        "toggle": toggle,
        "enforce": enforce,
    }


async def _run(label: str, step, outcomes: dict[str, str]) -> None:
    try:
        await step()
        outcomes[label] = "committed"
    except HTTPException as exc:
        outcomes[label] = f"answered {exc.status_code}"
    except Exception as exc:
        outcomes[label] = f"aborted: {type(exc).__name__}: {getattr(exc, 'orig', exc)!r}"[:300]


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ORDERS, ids=["-".join(order) for order in ORDERS])
async def test_release_admin_wide_template_toggle_and_enforcement_overlapping_in_any_order_never_deadlock_or_lose_a_write(
    monkeypatch: pytest.MonkeyPatch, recorder_db, pushes, order
):
    seed = await _seed(recorder_db)
    before = await _statuses(recorder_db, seed.users)
    _hold_after(monkeypatch, connection_enforcement, "_lock_user_rows")
    _hold_after(monkeypatch, admin_operation, "close_visible_restrictions")
    _hold_after(monkeypatch, user_operation, "close_active_restriction")
    _hold_after(monkeypatch, connection_limiter, "restrict")
    steps = _steps(recorder_db, seed)
    outcomes: dict[str, str] = {}
    deadlocks_before = await _deadlocks()

    tasks = []
    for label in order:
        tasks.append(asyncio.create_task(_run(label, steps[label], outcomes)))
        await asyncio.sleep(STAGGER_SECONDS)
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=BOUND)
    await asyncio.sleep(1)
    deadlocks_after = await _deadlocks()

    assert set(outcomes) == set(OPS), outcomes
    assert all(outcome in ("committed", "answered 503") for outcome in outcomes.values()), outcomes
    if deadlocks_before is not None:
        assert deadlocks_after - deadlocks_before == 0, (order, outcomes)

    after = await _statuses(recorder_db, seed.users)
    async with recorder_db() as session:
        in_force = (
            await session.execute(
                select(ConnectionRestriction.user_id, User.status)
                .join(User, User.id == ConnectionRestriction.user_id)
                .where(ConnectionRestriction.active.is_(True), User.id.in_(list(seed.users)))
            )
        ).all()
        renewed = (
            await session.execute(select(User.used_traffic, User.data_limit).where(User.id == seed.renewed))
        ).one()
        resets = (
            (await session.execute(select(UserUsageResetLogs.id).where(UserUsageResetLogs.user_id == seed.renewed)))
            .scalars()
            .all()
        )
    assert all(status == UserStatus.disabled for _user_id, status in in_force), (in_force, after, outcomes)
    if outcomes["template"] == "committed":
        assert (renewed.used_traffic, renewed.data_limit, len(resets)) == (0, TEMPLATE_DATA_LIMIT, 1), outcomes
    for user_id, status in after.items():
        if status != before[user_id]:
            told = [pushed for _source, pushed_id, pushed in pushes if pushed_id == user_id]
            assert status in told, (user_id, before[user_id], status, told, outcomes)


async def _nowait_row(user_id: int) -> str:
    engine = create_async_engine(_database_url(), poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            try:
                await conn.execute(text("SELECT id FROM users WHERE id = :id FOR UPDATE NOWAIT"), {"id": user_id})
                return "free"
            except DBAPIError:
                return "held"
            finally:
                await conn.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled", [True, False], ids=["disable-a-disabled-user", "enable-an-active-user"])
async def test_a_toggle_that_changes_nothing_releases_the_user_row_before_it_pushes_to_the_nodes(
    monkeypatch: pytest.MonkeyPatch, recorder_db, disabled
):
    _, (user_id,), _ = await seed_users_and_nodes(recorder_db, user_count=1, node_count=0)
    status = UserStatus.disabled if disabled else UserStatus.active
    async with recorder_db() as session:
        await session.execute(update(User).where(User.id == user_id).values(status=status))
        await session.commit()
    seen: list[str] = []

    async def push_and_probe(db_user):
        seen.append(await _nowait_row(db_user.id))

    monkeypatch.setattr(user_operation, "sync_user", push_and_probe)

    async with recorder_db() as db:
        db_user = await get_user_by_id(db, user_id, load_admin_role=True)
        await UserOperation(OperatorType.API)._set_user_disabled(
            db, db_user, UserStatusToggle(disabled=disabled), OWNER
        )

    assert seen == ["free"], "the toggle held the user row while it pushed to the nodes"
    assert (await _statuses(recorder_db, [user_id]))[user_id] == status
