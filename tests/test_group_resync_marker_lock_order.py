from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Admin, AdminRole, Base, Group, ProxyInbound, User
from app.models.group import BulkGroup, BulkGroupSelection, GroupModify
from app.models.proxy import ProxyTable
from app.operation import OperatorType
from app.operation.group import GroupOperation

OWNER = SimpleNamespace(username="marker-owner", is_owner=True, role=None)
VLESS = "marker-vless"


def _foreign_keys_on(dbapi_connection, _record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


@pytest_asyncio.fixture
async def engine(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'marker.db'}")
    event.listen(engine.sync_engine, "connect", _foreign_keys_on)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def sessions(engine):
    return async_sessionmaker(engine, class_=AsyncSession, autoflush=False, expire_on_commit=False)


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    from app import notification
    from app.fork.proxy_secrets import group_repair
    from app.operation import group as group_module

    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(group_module, "sync_users", nothing)
    monkeypatch.setattr(group_repair, "sync_users", nothing)
    for name in ("modify_group", "remove_group"):
        monkeypatch.setattr(notification, name, nothing)


async def _seed(sessions):
    async with sessions() as db:
        role = AdminRole(name="marker-owner", is_owner=True)
        db.add(role)
        await db.flush()
        admin = Admin(username="marker-owner", hashed_password="x", role_id=role.id)
        db.add(admin)
        inbound = ProxyInbound(tag=VLESS)
        db.add(inbound)
        await db.flush()
        group = Group(name="marker-group", inbounds=[inbound])
        other = Group(name="marker-other", inbounds=[inbound])
        db.add_all([group, other])
        await db.flush()
        user = User(username="marker-member", proxy_settings=ProxyTable().dict(), admin_id=admin.id)
        user.groups = [group]
        db.add(user)
        await db.commit()
        return SimpleNamespace(group_id=group.id, other_id=other.id, user_id=user.id)


def _statements(engine):
    seen = []

    def record(conn, cursor, statement, *rest):
        text = " ".join(statement.split()).lower()
        if "named_locks" in text and text.startswith("update"):
            seen.append("lock")
        elif text.startswith("insert into pending_user_resyncs"):
            seen.append("marker")

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    return seen


def _operation():
    operation = GroupOperation(operator_type=OperatorType.API)

    async def accept_tags(tags):
        return None

    operation.check_inbound_tags = accept_tags
    return operation


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["modify_group", "bulk_add_groups", "bulk_set_groups_disabled", "remove_group"])
async def test_a_group_change_takes_the_secrets_lock_before_it_writes_resync_markers(engine, sessions, path):
    seed = await _seed(sessions)
    seen = _statements(engine)
    operation = _operation()

    async with sessions() as db:
        if path == "modify_group":
            await operation.modify_group(db, seed.group_id, GroupModify(name="marker-group", inbound_tags=[VLESS]), OWNER)
        elif path == "bulk_add_groups":
            await operation.bulk_add_groups(db, BulkGroup(group_ids={seed.other_id}, users={seed.user_id}), OWNER)
        elif path == "bulk_set_groups_disabled":
            await operation.bulk_set_groups_disabled(
                db, BulkGroupSelection(ids={seed.group_id}), OWNER, is_disabled=True
            )
        else:
            await operation.remove_group(db, seed.group_id, OWNER)

    assert "marker" in seen, seen
    assert seen.index("lock") < seen.index("marker"), f"resync markers were written before the lock: {seen}"
