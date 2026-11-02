from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.settings as settings_module
from app import notification
from app.db import base
from app.db.models import Admin, AdminRole, User, UserStatus
from app.models.admin import AdminDetails, AdminRoleData
from app.models.proxy import ProxyTable
from app.operation import OperatorType, admin as admin_operation
from app.operation.admin import AdminOperation
from app.operation.user import UserOperation
from app.utils import connection_enforcement

OWNER = AdminDetails(id=None, username="owner", role=AdminRoleData(id=1, name="owner", is_owner=True))
CASES = {
    "disable_all": ("_disable_all_active_users_for_admin", UserStatus.active),
    "activate_all": ("_activate_all_disabled_users_for_admin", UserStatus.disabled),
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
    monkeypatch.setattr(admin_operation, "sync_users", AsyncMock())
    yield
    await settings_module.refresh_caches()


@pytest.mark.parametrize("action", sorted(CASES))
async def test_an_admin_wide_change_leaves_a_user_moved_to_another_admin_mid_change_alone_on_sqlite(
    tmp_path, monkeypatch: pytest.MonkeyPatch, action: str
):
    method, start_status = CASES[action]
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'moved.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(base.Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        session.add(AdminRole(name="owner", is_owner=True, permissions={}, limits={}, features={}, access={}))
        session.add(AdminRole(name="administrator", is_owner=False, permissions={}, limits={}, features={}, access={}))
        session.add(AdminRole(name="operator", is_owner=False, permissions={}, limits={}, features={}, access={}))
        await session.flush()
        admin_a = Admin(username="admin-a", hashed_password="secret", role_id=3)
        admin_b = Admin(username="admin-b", hashed_password="secret", role_id=3)
        session.add_all([admin_a, admin_b])
        await session.flush()
        stays = User(
            username="stays", admin_id=admin_a.id, status=start_status, proxy_settings=ProxyTable().dict(no_obj=True)
        )
        moves = User(
            username="moves", admin_id=admin_a.id, status=start_status, proxy_settings=ProxyTable().dict(no_obj=True)
        )
        session.add_all([stays, moves])
        await session.commit()
        admin_a_id, admin_b_id, stays_id, moves_id = admin_a.id, admin_b.id, stays.id, moves.id

    real_close = connection_enforcement.close_active_restrictions

    async def move_then_close(db, user_ids, reason):
        async with factory() as mover:
            await mover.execute(update(User).where(User.id == moves_id).values(admin_id=admin_b_id))
            await mover.commit()
        return await real_close(db, user_ids, reason)

    monkeypatch.setattr("app.fork.operation.admin_scope.close_active_restrictions", move_then_close)

    async with factory() as db:
        db_admin = await db.get(Admin, admin_a_id)
        await getattr(AdminOperation(OperatorType.API), method)(db, db_admin, OWNER)

    async with factory() as check:
        rows = dict((await check.execute(select(User.id, User.status))).all())
        owners = dict((await check.execute(select(User.id, User.admin_id))).all())
    await engine.dispose()

    assert owners[moves_id] == admin_b_id
    assert rows[stays_id] != start_status
    assert rows[moves_id] == start_status, (action, rows)
