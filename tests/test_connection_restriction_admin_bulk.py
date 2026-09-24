from datetime import UTC, datetime as dt, timedelta as td
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select

from app import notification
from app.db.models import User, UserStatus
from app.fork.models.connection import ConnectionRestriction
from app.models.user import BulkUsersSelection
from app.operation import OperatorType, user as user_module
from app.operation.user import UserOperation
from tests.a1_bridge_harness import seed_group, seed_user
from tests.api import TestSession
from tests.api.helpers import unique_name

ADMIN = SimpleNamespace(username="owner", is_owner=True, role=None, id=None)


async def _restricted_user(**fields) -> int:
    user_id = await seed_user(await seed_group(unique_name("bulk_tag")), **fields)
    async with TestSession() as session:
        session.add(
            ConnectionRestriction(
                user_id=user_id,
                previous_status=UserStatus.active.value,
                disable_minutes=30,
                restore_at=dt.now(UTC) + td(minutes=30),
                active=True,
            )
        )
        await session.commit()
    return user_id


async def _state(user_id: int):
    async with TestSession() as session:
        status = (await session.execute(select(User.status).where(User.id == user_id))).scalar_one()
        active = (
            await session.execute(select(ConnectionRestriction.active).where(ConnectionRestriction.user_id == user_id))
        ).scalar_one()
    return status, active


def _quiet(monkeypatch):
    monkeypatch.setattr(user_module, "sync_users", AsyncMock())
    monkeypatch.setattr(UserOperation, "generate_subscription_url", staticmethod(AsyncMock(return_value="")))
    monkeypatch.setattr(notification, "user_status_change", AsyncMock())


async def test_a_bulk_disable_of_an_already_disabled_user_closes_its_running_restriction(monkeypatch):
    _quiet(monkeypatch)
    user_id = await _restricted_user(status=UserStatus.disabled)

    async with TestSession() as db:
        await UserOperation(operator_type=OperatorType.API).bulk_disable_users(
            db, BulkUsersSelection(ids={user_id}), ADMIN
        )

    assert await _state(user_id) == (UserStatus.disabled, False)


async def test_a_bulk_enable_closes_the_restriction_of_the_user_it_enables(monkeypatch):
    _quiet(monkeypatch)
    user_id = await _restricted_user(status=UserStatus.disabled)

    async with TestSession() as db:
        await UserOperation(operator_type=OperatorType.API).bulk_enable_users(
            db, BulkUsersSelection(ids={user_id}), ADMIN
        )

    assert await _state(user_id) == (UserStatus.active, False)


async def test_a_single_disable_of_an_already_disabled_user_closes_its_running_restriction(monkeypatch):
    from app.db.crud.user import get_user_by_id
    from app.models.user import UserStatusToggle

    _quiet(monkeypatch)
    monkeypatch.setattr(user_module, "sync_user", AsyncMock())
    user_id = await _restricted_user(status=UserStatus.disabled)

    async with TestSession() as db:
        db_user = await get_user_by_id(db, user_id)
        await UserOperation(operator_type=OperatorType.API)._set_user_disabled(
            db, db_user, UserStatusToggle(disabled=True), ADMIN
        )

    assert await _state(user_id) == (UserStatus.disabled, False)
