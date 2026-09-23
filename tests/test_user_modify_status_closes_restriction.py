from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.crud.user import get_user_by_id
from app.db.models import Admin, AdminRole, Base, ConnectionRestriction, User, UserStatus
from app.fork.jobs import connection_limiter
from app.models.proxy import ProxyTable
from app.models.settings import ConnectionLimit
from app.models.user import UserModify
from app.operation import OperatorType
from app.operation.user import UserOperation

PREFIX = "rst-"
OWNER = SimpleNamespace(username=f"{PREFIX}owner", is_owner=True, role=None, id=None)
ENFORCING = ConnectionLimit(enforcement_enabled=True, monitor_only=False, persistence_cycles=1, punishment_steps=[30])


def _foreign_keys_on(dbapi_connection, _record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


@pytest_asyncio.fixture
async def sessions(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'restriction.db'}")
    event.listen(engine.sync_engine, "connect", _foreign_keys_on)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, autoflush=False, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    from app import notification
    from app.operation import user as user_module

    async def nothing(*args, **kwargs):
        return None

    async def no_url(*args, **kwargs):
        return ""

    monkeypatch.setattr(user_module, "sync_user", nothing)
    monkeypatch.setattr(UserOperation, "generate_subscription_url", staticmethod(no_url))
    for name in ("modify_user", "user_status_change"):
        monkeypatch.setattr(notification, name, nothing)


async def _user_disabled_by_the_limiter(sessions):
    async with sessions() as db:
        role = AdminRole(name=f"{PREFIX}owner", is_owner=True)
        db.add(role)
        await db.flush()
        admin = Admin(username=f"{PREFIX}owner", hashed_password="x", role_id=role.id)
        db.add(admin)
        await db.flush()
        user = User(username=f"{PREFIX}member", proxy_settings=ProxyTable().dict(), admin_id=admin.id)
        user.status = UserStatus.disabled
        user.last_status_change = datetime.now(UTC) - timedelta(minutes=5)
        db.add(user)
        await db.flush()
        db.add(
            ConnectionRestriction(
                user_id=user.id,
                previous_status=UserStatus.active.value,
                created_at=datetime.now(UTC) - timedelta(minutes=5),
                disable_minutes=30,
                restore_at=datetime.now(UTC) - timedelta(seconds=1),
                active=True,
            )
        )
        await db.commit()
        return user.id


async def _release(sessions):
    pushed = AsyncMock()
    with patch.object(connection_limiter, "_push_to_nodes", new=pushed):
        async with sessions() as db:
            await connection_limiter._release_due(db, ENFORCING)
    return [user.id for call in pushed.await_args_list for user in call.args[0]]


@pytest.mark.asyncio
async def test_an_admin_status_set_by_a_user_edit_outlives_the_restriction_it_overlapped(sessions):
    user_id = await _user_disabled_by_the_limiter(sessions)

    async with sessions() as db:
        db_user = await get_user_by_id(db, user_id, load_admin_role=True)
        answer = await UserOperation(operator_type=OperatorType.API)._modify_user(
            db, db_user, UserModify(status=UserStatus.disabled), OWNER, skip_role_limits=True
        )
    assert answer.status == UserStatus.disabled

    pushed = await _release(sessions)

    async with sessions() as db:
        status = await db.scalar(select(User.status).where(User.id == user_id))
        active = await db.scalar(select(ConnectionRestriction.active).where(ConnectionRestriction.user_id == user_id))
    assert status == UserStatus.disabled
    assert active is False
    assert pushed == []


@pytest.mark.asyncio
async def test_a_user_edit_that_leaves_the_status_alone_keeps_the_restriction_running(sessions):
    user_id = await _user_disabled_by_the_limiter(sessions)

    async with sessions() as db:
        db_user = await get_user_by_id(db, user_id, load_admin_role=True)
        await UserOperation(operator_type=OperatorType.API)._modify_user(
            db, db_user, UserModify(note="checked"), OWNER, skip_role_limits=True
        )

    async with sessions() as db:
        active = await db.scalar(select(ConnectionRestriction.active).where(ConnectionRestriction.user_id == user_id))
    assert active is True
