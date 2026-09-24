from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.crud.admin import get_admin_by_id
from app.db.models import Base, User, UserStatus
from app.models.admin import AdminDetails, AdminRoleData
from app.operation import OperatorType, admin as admin_operation
from tests.test_connection_restriction_admin_decision import (
    ENFORCING,
    _expire_restrictions,
    _pushed_ids,
    _release,
    _restrict_now,
    _seed,
    _status_and_restriction,
)

OWNER = AdminDetails(id=None, username="owner", role=AdminRoleData(id=1, name="owner", is_owner=True))


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
def no_node_push(monkeypatch):
    monkeypatch.setattr(admin_operation, "sync_users", AsyncMock())


async def _admin_changes_all(session_factory, admin_id: int, action: str) -> None:
    operation = admin_operation.AdminOperation(OperatorType.API)
    async with session_factory() as db:
        db_admin = await get_admin_by_id(db, admin_id)
        await getattr(operation, action)(db, db_admin, OWNER)


@pytest.mark.asyncio
async def test_a_user_restricted_while_its_admin_disables_every_user_stays_disabled_after_release(session_factory):
    [(admin_id, restricted_id), (_, free_id)] = await _seed(session_factory, users_per_owner=2)
    await _restrict_now(session_factory, restricted_id)

    await _admin_changes_all(session_factory, admin_id, "_disable_all_active_users_for_admin")
    await _expire_restrictions(session_factory)
    pushed = await _release(session_factory, ENFORCING)

    assert await _status_and_restriction(session_factory, restricted_id) == (UserStatus.disabled, False)
    async with session_factory() as db:
        assert (await db.execute(select(User.status).where(User.id == free_id))).scalar_one() == UserStatus.disabled
    assert _pushed_ids(pushed) == []


@pytest.mark.asyncio
async def test_activating_every_user_closes_the_restriction_so_its_release_cannot_undo_it(session_factory):
    [(admin_id, restricted_id)] = await _seed(session_factory)
    await _restrict_now(session_factory, restricted_id)

    await _admin_changes_all(session_factory, admin_id, "_activate_all_disabled_users_for_admin")

    assert await _status_and_restriction(session_factory, restricted_id) == (UserStatus.active, False)
    await _expire_restrictions(session_factory)
    pushed = await _release(session_factory, ENFORCING)
    assert await _status_and_restriction(session_factory, restricted_id) == (UserStatus.active, False)
    assert _pushed_ids(pushed) == []
