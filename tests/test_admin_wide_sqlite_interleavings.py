from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.settings as settings_module
from app import notification
from app.db import base
from app.db.models import Admin, AdminRole, Group, User, UserStatus, users_groups_association
from app.fork.models.connection import ConnectionRestriction
from app.models.admin import AdminDetails, AdminRoleData
from app.models.admin_role import RoleAccess
from app.models.proxy import ProxyTable
from app.operation import OperatorType, admin as admin_operation
from app.operation.admin import AdminOperation
from app.operation.user import UserOperation
from app.utils import connection_enforcement

OWNER = AdminDetails(id=None, username="owner", role=AdminRoleData(id=1, name="owner", is_owner=True))
CASES = {
    "disable_all": ("_disable_all_active_users_for_admin", UserStatus.active, UserStatus.disabled),
    "activate_all": ("_activate_all_disabled_users_for_admin", UserStatus.disabled, UserStatus.active),
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
    pushed = AsyncMock()
    monkeypatch.setattr(admin_operation, "sync_users", pushed)
    yield pushed
    await settings_module.refresh_caches()


async def _world(tmp_path, specs: dict[str, tuple[str, UserStatus, list[str]]]):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'admin-wide.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(base.Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        session.add(AdminRole(name="owner", is_owner=True, permissions={}, limits={}, features={}, access={}))
        session.add(AdminRole(name="administrator", is_owner=False, permissions={}, limits={}, features={}, access={}))
        session.add(AdminRole(name="operator", is_owner=False, permissions={}, limits={}, features={}, access={}))
        await session.flush()
        admins = {name: Admin(username=f"admin-{name}", hashed_password="secret", role_id=3) for name in ("a", "b")}
        groups = {name: Group(name=name, inbounds=[]) for name in ("g1", "g2")}
        session.add_all([*admins.values(), *groups.values()])
        await session.flush()
        users = {
            name: User(
                username=name, admin_id=admins[owner].id, status=status, proxy_settings=ProxyTable().dict(no_obj=True)
            )
            for name, (owner, status, _) in specs.items()
        }
        session.add_all(users.values())
        await session.flush()
        for name, (_, _, group_names) in specs.items():
            for group_name in group_names:
                await session.execute(
                    users_groups_association.insert().values(user_id=users[name].id, groups_id=groups[group_name].id)
                )
        await session.commit()
        ids = SimpleNamespace(
            admins={name: admin.id for name, admin in admins.items()},
            groups={name: group.id for name, group in groups.items()},
            users={name: user.id for name, user in users.items()},
        )
    return engine, factory, ids


def _interfere_at_close(monkeypatch: pytest.MonkeyPatch, factory, statements):
    real_close = connection_enforcement.close_active_restrictions

    async def interfere_then_close(db, user_ids, reason):
        async with factory() as other:
            for statement in statements:
                await other.execute(statement)
            await other.commit()
        return await real_close(db, user_ids, reason)

    monkeypatch.setattr("app.fork.operation.admin_scope.close_active_restrictions", interfere_then_close)


async def _run(factory, admin_id: int, method: str, caller: AdminDetails) -> None:
    async with factory() as db:
        db_admin = await db.get(Admin, admin_id)
        await getattr(AdminOperation(OperatorType.API), method)(db, db_admin, caller)


async def _state(factory):
    async with factory() as check:
        statuses = dict((await check.execute(select(User.id, User.status))).all())
        owners = dict((await check.execute(select(User.id, User.admin_id))).all())
        restrictions = dict(
            (await check.execute(select(ConnectionRestriction.user_id, ConnectionRestriction.active))).all()
        )
    return statuses, owners, restrictions


def _pushed(pushed: AsyncMock) -> dict[int, tuple[UserStatus, int]]:
    assert pushed.await_count == 1
    return {user.id: (user.status, user.admin_id) for user in pushed.await_args.args[0]}


@pytest.mark.parametrize("action", sorted(CASES))
async def test_a_user_moved_into_the_admin_mid_change_is_left_alone_and_pushed_as_committed(
    tmp_path, monkeypatch: pytest.MonkeyPatch, quiet, action: str
):
    method, start, after = CASES[action]
    engine, factory, ids = await _world(tmp_path, {"stays": ("a", start, []), "joins": ("b", start, [])})
    _interfere_at_close(
        monkeypatch,
        factory,
        [update(User).where(User.id == ids.users["joins"]).values(admin_id=ids.admins["a"])],
    )
    await _run(factory, ids.admins["a"], method, OWNER)
    statuses, owners, _ = await _state(factory)
    await engine.dispose()

    assert owners[ids.users["joins"]] == ids.admins["a"]
    assert statuses[ids.users["stays"]] == after
    assert statuses[ids.users["joins"]] == start
    pushed = _pushed(quiet)
    for user_id, (status, owner) in pushed.items():
        assert (status, owner) == (statuses[user_id], owners[user_id]), (user_id, pushed)
    assert set(pushed) == {ids.users["stays"], ids.users["joins"]}


@pytest.mark.parametrize("action", sorted(CASES))
async def test_a_user_deleted_mid_change_neither_fails_the_change_nor_is_pushed(
    tmp_path, monkeypatch: pytest.MonkeyPatch, quiet, action: str
):
    method, start, after = CASES[action]
    engine, factory, ids = await _world(tmp_path, {"stays": ("a", start, []), "goes": ("a", start, [])})
    _interfere_at_close(monkeypatch, factory, [delete(User).where(User.id == ids.users["goes"])])
    await _run(factory, ids.admins["a"], method, OWNER)
    statuses, _, _ = await _state(factory)
    await engine.dispose()

    assert ids.users["goes"] not in statuses
    assert statuses[ids.users["stays"]] == after
    assert set(_pushed(quiet)) == {ids.users["stays"]}


@pytest.mark.parametrize("action", sorted(CASES))
async def test_a_group_scoped_change_keeps_its_group_and_admin_checks_mid_change_on_sqlite(
    tmp_path, monkeypatch: pytest.MonkeyPatch, quiet, action: str
):
    method, start, after = CASES[action]
    engine, factory, ids = await _world(
        tmp_path,
        {
            "inside": ("a", start, ["g1"]),
            "outside": ("a", start, ["g2"]),
            "leaves": ("a", start, ["g1"]),
            "moves": ("a", start, ["g1"]),
        },
    )
    caller = AdminDetails(
        id=None,
        username="scoped",
        role=AdminRoleData(
            id=3, name="operator", is_owner=False, access=RoleAccess(allowed_group_ids=[ids.groups["g1"]])
        ),
    )
    _interfere_at_close(
        monkeypatch,
        factory,
        [
            delete(users_groups_association).where(
                users_groups_association.c.user_id == ids.users["leaves"],
                users_groups_association.c.groups_id == ids.groups["g1"],
            ),
            update(User).where(User.id == ids.users["moves"]).values(admin_id=ids.admins["b"]),
        ],
    )
    await _run(factory, ids.admins["a"], method, caller)
    statuses, owners, _ = await _state(factory)
    await engine.dispose()

    assert statuses[ids.users["inside"]] == after
    assert statuses[ids.users["outside"]] == start
    assert statuses[ids.users["leaves"]] == start
    assert statuses[ids.users["moves"]] == start
    pushed = _pushed(quiet)
    for user_id, (status, owner) in pushed.items():
        assert (status, owner) == (statuses[user_id], owners[user_id]), (user_id, pushed)
    assert ids.users["outside"] not in pushed


async def test_a_limiter_strike_between_the_restriction_close_and_the_status_write_on_sqlite(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    method, _, after = CASES["disable_all"]
    engine, factory, ids = await _world(
        tmp_path, {"stays": ("a", UserStatus.active, []), "victim": ("a", UserStatus.active, [])}
    )
    real_bulk = admin_operation.disable_all_active_users
    outcome: list[str] = []

    async def limiter_strikes_then_bulk(**kwargs):
        try:
            async with factory() as limiter:
                await limiter.connection(execution_options={})
                limiter.add(
                    ConnectionRestriction(
                        user_id=ids.users["victim"],
                        previous_status=UserStatus.active.value,
                        disable_minutes=30,
                        restore_at=datetime.now(UTC) - timedelta(seconds=1),
                        active=True,
                    )
                )
                await limiter.execute(
                    update(User).where(User.id == ids.users["victim"]).values(status=UserStatus.disabled)
                )
                await limiter.commit()
            outcome.append("limiter committed inside the window")
        except OperationalError as exc:
            outcome.append(f"limiter blocked: {exc.orig}")
        return await real_bulk(**kwargs)

    monkeypatch.setattr(admin_operation, "disable_all_active_users", limiter_strikes_then_bulk)
    await _run(factory, ids.admins["a"], method, OWNER)
    async with factory() as job:
        pairs = await connection_enforcement.due_to_restore(job)
        restored = [user.id for restriction, user in pairs if connection_enforcement.release(restriction, user)]
        await job.commit()
    statuses, _, restrictions = await _state(factory)
    await engine.dispose()

    assert statuses[ids.users["stays"]] == after
    assert statuses[ids.users["victim"]] == UserStatus.disabled, (outcome, restored, statuses, restrictions)
