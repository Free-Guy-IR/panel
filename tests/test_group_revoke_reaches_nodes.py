from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import event, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Admin, AdminRole, Base, CoreConfig, Group, ProxyInbound, User
from app.models.core import CoreType
from app.models.group import BulkGroupSelection
from app.models.proxy import ProxyTable
from app.node.user import serialize_users_for_node
from app.operation import OperatorType
from app.operation.group import GroupOperation

PREFIX = "a3r-"
WG_TAG = f"{PREFIX}wg"
VLESS = f"{PREFIX}vless"
OWNER = SimpleNamespace(username=f"{PREFIX}owner", is_owner=True, role=None)


class _DriverError(Exception):
    def __init__(self, code, message):
        super().__init__(code, message)
        self.sqlstate = None


LOCK_WAIT_TIMEOUT = OperationalError("SELECT wireguard_subnets FOR UPDATE", {}, _DriverError(1205, "Lock wait timeout"))


def _foreign_keys_on(dbapi_connection, _record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


@pytest_asyncio.fixture
async def engine(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'a3r.db'}")
    event.listen(engine.sync_engine, "connect", _foreign_keys_on)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def sessions(engine):
    return async_sessionmaker(engine, class_=AsyncSession, autoflush=False, expire_on_commit=False)


@pytest_asyncio.fixture
async def repair(monkeypatch, sessions):
    from app import notification
    from app.fork.proxy_secrets import group_repair
    from app.operation import group as group_module

    state = SimpleNamespace(pushed=[], tasks=[])

    class _TestGetDB:
        def __init__(self):
            self.db = sessions()

        async def __aenter__(self):
            return self.db

        async def __aexit__(self, exc_type, exc_value, traceback):
            if exc_type is not None:
                await self.db.rollback()
            await self.db.close()

    async def record(users):
        for proto in await serialize_users_for_node(list(users)):
            state.pushed.append((int(proto.email), list(proto.inbounds)))

    async def nothing(*args, **kwargs):
        return None

    async def contended(db, users, **kwargs):
        raise LOCK_WAIT_TIMEOUT

    real_schedule = group_repair.schedule_allocation_repair

    def schedule(user_ids):
        task = real_schedule(user_ids)
        if task is not None:
            state.tasks.append(task)
        return task

    monkeypatch.setattr(group_module, "sync_users", record)
    monkeypatch.setattr(group_repair, "sync_users", record)
    monkeypatch.setattr(group_repair, "sync_users_allocations", contended)
    monkeypatch.setattr(group_repair, "GetDB", _TestGetDB)
    monkeypatch.setattr(group_repair, "IN_REQUEST_RETRY_PAUSE_SECONDS", 0.01)
    monkeypatch.setattr(group_repair, "REPAIR_DELAYS_SECONDS", (0.01, 0.01, 0.01))
    monkeypatch.setattr(group_repair, "schedule_allocation_repair", schedule)
    monkeypatch.setattr(group_repair, "_pending_repairs", {})
    monkeypatch.setattr(group_repair, "_repair_owners", {})
    monkeypatch.setattr(group_repair, "_dirty_user_ids", set())
    for name in ("modify_group", "remove_group"):
        monkeypatch.setattr(notification, name, nothing)
    yield state
    await group_repair.cancel_pending_repairs()


async def _seed(sessions):
    async with sessions() as db:
        role = AdminRole(name=f"{PREFIX}owner", is_owner=True)
        db.add(role)
        await db.flush()
        admin = Admin(username=f"{PREFIX}owner", hashed_password="x", role_id=role.id)
        db.add(admin)
        wg_inbound = ProxyInbound(tag=WG_TAG)
        vless_inbound = ProxyInbound(tag=VLESS)
        db.add_all([wg_inbound, vless_inbound])
        await db.flush()
        db.add(
            CoreConfig(
                name=f"{PREFIX}wg",
                type=CoreType.wg,
                config={"interface_name": WG_TAG, "address": ["10.80.0.1/24"], "listen_port": 51820},
            )
        )
        group = Group(name=f"{PREFIX}paid", inbounds=[wg_inbound, vless_inbound])
        db.add(group)
        await db.flush()
        user = User(username=f"{PREFIX}member", proxy_settings=ProxyTable().dict(), admin_id=admin.id)
        user.groups = [group]
        db.add(user)
        await db.commit()
        return SimpleNamespace(group_id=group.id, user_id=user.id)


@pytest.mark.asyncio
async def test_disabling_a_group_reaches_the_nodes_even_when_the_address_release_is_contended(sessions, repair):
    seed = await _seed(sessions)
    operation = GroupOperation(operator_type=OperatorType.API)

    async with sessions() as db:
        with pytest.raises(HTTPException) as raised:
            await operation.bulk_set_groups_disabled(
                db, BulkGroupSelection(ids={seed.group_id}), OWNER, is_disabled=True
            )
    assert raised.value.status_code == 503
    for task in repair.tasks:
        await task

    async with sessions() as db:
        group = await db.get(Group, seed.group_id)
        assert group.is_disabled is True

    pushes = [inbounds for user_id, inbounds in repair.pushed if user_id == seed.user_id]
    assert pushes and pushes[-1] == [], (
        f"group is disabled in the database but its member was never pushed without it (pushes: {pushes})"
    )


async def _markers(sessions):
    from app.fork.models.pending_user_resync import PendingUserResync

    async with sessions() as db:
        rows = await db.execute(select(PendingUserResync.user_id, PendingUserResync.token))
        return rows.all()


@pytest.mark.asyncio
async def test_a_group_change_that_cannot_finish_leaves_a_durable_marker_that_the_drain_job_finishes(
    sessions, monkeypatch, repair
):
    from app.fork.proxy_secrets import group_repair

    seed = await _seed(sessions)
    async with sessions() as db:
        await db.execute(update(Group).where(Group.id == seed.group_id).values(is_disabled=True))
        await db.commit()
    operation = GroupOperation(operator_type=OperatorType.API)
    monkeypatch.setattr(group_repair, "schedule_allocation_repair", lambda user_ids: None)

    async with sessions() as db:
        with pytest.raises(HTTPException) as raised:
            await operation.bulk_set_groups_disabled(
                db, BulkGroupSelection(ids={seed.group_id}), OWNER, is_disabled=False
            )
    assert raised.value.status_code == 503
    assert [user_id for user_id, _ in await _markers(sessions)] == [seed.user_id]

    monkeypatch.undo()
    pushed = []

    async def record(users):
        for proto in await serialize_users_for_node(list(users)):
            pushed.append((int(proto.email), sorted(proto.inbounds)))

    class _TestGetDB:
        def __init__(self):
            self.db = sessions()

        async def __aenter__(self):
            return self.db

        async def __aexit__(self, exc_type, exc_value, traceback):
            if exc_type is not None:
                await self.db.rollback()
            await self.db.close()

    monkeypatch.setattr(group_repair, "GetDB", _TestGetDB)
    monkeypatch.setattr(group_repair, "sync_users", record)

    assert await group_repair.drain_pending_resyncs(min_age_seconds=0) == 1

    assert await _markers(sessions) == []
    assert pushed == [(seed.user_id, sorted([WG_TAG, VLESS]))]
    async with sessions() as db:
        settings = await db.scalar(select(User.proxy_settings).where(User.id == seed.user_id))
    assert settings["wireguard"]["peer_ips"]


@pytest.mark.asyncio
async def test_a_group_change_that_finishes_leaves_no_marker(sessions, monkeypatch):
    from app import notification
    from app.operation import group as group_module

    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(group_module, "sync_users", nothing)
    for name in ("modify_group", "remove_group"):
        monkeypatch.setattr(notification, name, nothing)
    seed = await _seed(sessions)
    operation = GroupOperation(operator_type=OperatorType.API)

    async with sessions() as db:
        await operation.bulk_set_groups_disabled(db, BulkGroupSelection(ids={seed.group_id}), OWNER, is_disabled=True)

    assert await _markers(sessions) == []


@pytest.mark.asyncio
async def test_a_marker_chunk_that_keeps_failing_does_not_hold_back_the_ones_after_it(sessions, monkeypatch):
    from app.fork.proxy_secrets import group_repair, resync_markers

    seed = await _seed(sessions)
    async with sessions() as db:
        admin_id = await db.scalar(select(User.admin_id).where(User.id == seed.user_id))
        later = User(username=f"{PREFIX}later", proxy_settings=ProxyTable().dict(), admin_id=admin_id)
        db.add(later)
        await db.flush()
        later_id = later.id
        await resync_markers.mark_users(db, [seed.user_id, later_id])
        await db.commit()

    finished = []

    async def finish(user_ids, markers=None):
        if seed.user_id in user_ids:
            raise ValueError("WireGuard subnet 10.80.0.0/24 has no free addresses")
        finished.extend(user_ids)
        async with sessions() as db:
            await resync_markers.clear_markers(db, markers)
            await db.commit()

    class _TestGetDB:
        def __init__(self):
            self.db = sessions()

        async def __aenter__(self):
            return self.db

        async def __aexit__(self, *exc):
            await self.db.close()

    monkeypatch.setattr(group_repair, "GetDB", _TestGetDB)
    monkeypatch.setattr(group_repair, "DRAIN_CHUNK", 1)
    monkeypatch.setattr(group_repair, "finish_in_background", finish)

    assert await group_repair.drain_pending_resyncs(min_age_seconds=0) == 1

    assert finished == [later_id]
    assert [user_id for user_id, _ in await _markers(sessions)] == [seed.user_id]
