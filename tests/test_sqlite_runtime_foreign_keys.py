from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import base
from app.db.crud.node import remove_node
from app.db.crud.user import remove_user
from app.db.models import (
    Admin,
    AdminRole,
    Base,
    ConnectionRestriction,
    ContentFilterAssignment,
    ContentFilterProfile,
    Node,
    User,
    UserConnectionLimit,
    UserStatus,
    UserSubscriptionAccess,
)
from app.models.proxy import ProxyTable
from app.utils.connection_enforcement import active_restriction, due_to_restore, release


def _runtime_pragmas(dbapi_conn, record):
    base._set_sqlite_pragmas(dbapi_conn, record)


@pytest.fixture
async def runtime_db(tmp_path):
    if not hasattr(base, "_set_sqlite_pragmas"):
        pytest.skip(reason="the runtime engine is not SQLite, so the SQLite connect listener is not installed")
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'runtime.sqlite3'}", connect_args={"check_same_thread": False}
    )
    event.listen(engine.sync_engine, "connect", _runtime_pragmas)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


async def _user(db, admin_id, name, status):
    user = User(username=name, proxy_settings=ProxyTable().dict(), admin_id=admin_id)
    user.status = status
    db.add(user)
    await db.commit()
    return user


@pytest.mark.asyncio
async def test_runtime_sqlite_connection_enforces_foreign_keys(runtime_db):
    assert (await runtime_db.execute(select(func.count()).select_from(User))).scalar() == 0
    enabled = (await runtime_db.connection()).exec_driver_sql("PRAGMA foreign_keys")
    assert (await enabled).scalar() == 1


@pytest.mark.asyncio
async def test_deleted_user_state_is_not_inherited_by_the_next_user(runtime_db):
    db = runtime_db
    role = AdminRole(name=f"role-{uuid4().hex[:6]}")
    db.add(role)
    await db.commit()
    admin = Admin(username=f"owner-{uuid4().hex[:6]}", hashed_password="x", role_id=role.id)
    db.add(admin)
    await db.commit()

    old = await _user(db, admin.id, "old-user", UserStatus.disabled)
    db.add(UserConnectionLimit(user_id=old.id, exempt=True))
    db.add(
        ConnectionRestriction(
            user_id=old.id,
            previous_status="active",
            active=True,
            restore_at=datetime.now(UTC) - timedelta(minutes=1),
        )
    )
    db.add(
        UserSubscriptionAccess(user_id=old.id, access_kind="raw", user_agent="old-client", ip="198.51.100.7")
    )
    await db.commit()
    old_id = old.id

    await remove_user(db, old)

    fresh = await _user(db, admin.id, "fresh-user", UserStatus.disabled)

    leaked = {
        "same_id": fresh.id == old_id,
        "exempt_row": (
            await db.execute(select(func.count()).select_from(UserConnectionLimit).where(UserConnectionLimit.user_id == fresh.id))
        ).scalar(),
        "active_restriction": await active_restriction(db, fresh.id) is not None,
        "access_rows": (
            await db.execute(
                select(func.count()).select_from(UserSubscriptionAccess).where(UserSubscriptionAccess.user_id == fresh.id)
            )
        ).scalar(),
    }

    for restriction, user in await due_to_restore(db):
        release(restriction, user)
    await db.commit()
    await db.refresh(fresh)
    leaked["status_after_restore_job"] = fresh.status.value

    assert leaked == {
        "same_id": leaked["same_id"],
        "exempt_row": 0,
        "active_restriction": False,
        "access_rows": 0,
        "status_after_restore_job": "disabled",
    }


@pytest.mark.asyncio
async def test_deleted_node_filter_is_not_inherited_by_the_next_node(runtime_db):
    db = runtime_db

    def _node(name):
        return Node(
            name=name,
            address="127.0.0.1",
            port=62050,
            api_port=62051,
            server_ca="ca",
            api_key=str(uuid4()),
            core_config_id=None,
        )

    old = _node("old-node")
    db.add(old)
    await db.commit()
    profile = ContentFilterProfile(name="blocklist")
    db.add(profile)
    await db.commit()
    db.add(ContentFilterAssignment(profile_id=profile.id, node_id=old.id, inbound_tag=""))
    await db.commit()

    await remove_node(db, old)
    await db.commit()

    fresh = _node("fresh-node")
    db.add(fresh)
    await db.commit()

    inherited = (
        await db.execute(
            select(func.count()).select_from(ContentFilterAssignment).where(ContentFilterAssignment.node_id == fresh.id)
        )
    ).scalar()
    assert inherited == 0
