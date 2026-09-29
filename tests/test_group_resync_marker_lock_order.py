from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Admin, AdminRole, Base, Group, ProxyInbound, User, users_groups_association
from app.fork.models.pending_user_resync import PendingUserResync
from app.fork.proxy_secrets.locks import LOCK_UNAVAILABLE_DETAIL, NamedLockMissing
from app.models.group import BulkGroup, BulkGroupSelection, GroupModify
from app.models.proxy import ProxyTable
from app.operation import OperatorType
from app.operation.group import GroupOperation

OWNER = SimpleNamespace(username="marker-owner", is_owner=True, role=None)
VLESS = "marker-vless"
PATHS = [
    "modify_group",
    "remove_group",
    "bulk_add_groups",
    "bulk_remove_groups",
    "bulk_remove_groups_by_id",
    "bulk_set_groups_disabled",
]
WRITTEN_TABLES = ("users", "groups", "users_groups_association", "inbounds_groups_association", "pending_user_resyncs")


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
    monkeypatch.setattr(group_repair, "IN_REQUEST_RETRY_PAUSE_SECONDS", 0.01)
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
        if text.startswith("update named_locks"):
            seen.append("lock")
            return
        for verb, preposition in (("insert", "into "), ("update", ""), ("delete", "from ")):
            if not text.startswith(verb):
                continue
            target = text[len(verb) :].strip()
            if preposition and target.startswith(preposition):
                target = target[len(preposition) :]
            if target.split(" ", 1)[0].strip('"`') in WRITTEN_TABLES:
                seen.append(f"{verb} {target.split(' ', 1)[0]}")

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    return seen


def _operation():
    operation = GroupOperation(operator_type=OperatorType.API)

    async def accept_tags(tags):
        return None

    operation.check_inbound_tags = accept_tags
    return operation


async def _run(operation, db, seed, path):
    if path == "modify_group":
        return await operation.modify_group(
            db, seed.group_id, GroupModify(name="marker-renamed", inbound_tags=[VLESS]), OWNER
        )
    if path == "remove_group":
        return await operation.remove_group(db, seed.group_id, OWNER)
    if path == "bulk_add_groups":
        return await operation.bulk_add_groups(db, BulkGroup(group_ids={seed.other_id}, users={seed.user_id}), OWNER)
    if path == "bulk_remove_groups":
        return await operation.bulk_remove_groups(db, BulkGroup(group_ids={seed.group_id}, users={seed.user_id}), OWNER)
    if path == "bulk_remove_groups_by_id":
        return await operation.bulk_remove_groups_by_id(db, BulkGroupSelection(ids={seed.group_id}), OWNER)
    return await operation.bulk_set_groups_disabled(
        db, BulkGroupSelection(ids={seed.group_id}), OWNER, is_disabled=True
    )


async def _stored(sessions):
    association = users_groups_association
    async with sessions() as db:
        groups = (await db.execute(select(Group.id, Group.name, Group.is_disabled).order_by(Group.id))).all()
        members = (
            await db.execute(
                select(association.c.user_id, association.c.groups_id).order_by(
                    association.c.user_id, association.c.groups_id
                )
            )
        ).all()
        markers = (await db.execute(select(PendingUserResync.__table__.c.user_id))).scalars().all()
    return SimpleNamespace(
        groups=[tuple(row) for row in groups], members=[tuple(row) for row in members], markers=markers
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_a_group_change_takes_the_secrets_lock_before_it_writes_any_row(engine, sessions, path):
    seed = await _seed(sessions)
    seen = _statements(engine)

    async with sessions() as db:
        await _run(_operation(), db, seed, path)

    writes = [statement for statement in seen if statement != "lock"]
    assert "insert pending_user_resyncs" in writes, seen
    assert "lock" in seen, seen
    assert seen.index("lock") < seen.index(writes[0]), f"rows were written before the secrets lock: {seen}"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_a_group_change_that_cannot_take_the_secrets_lock_stores_nothing(sessions, monkeypatch, path):
    from app.fork.proxy_secrets import group_repair

    seed = await _seed(sessions)
    before = await _stored(sessions)
    scheduled = []

    async def unavailable(db):
        raise NamedLockMissing("the 'proxy_secrets' named lock row could not be taken")

    monkeypatch.setattr(group_repair, "acquire_proxy_secrets_lock", unavailable)
    monkeypatch.setattr(group_repair, "schedule_allocation_repair", lambda user_ids: scheduled.append(user_ids))

    async with sessions() as db:
        with pytest.raises(HTTPException) as refused:
            await _run(_operation(), db, seed, path)

    assert refused.value.status_code == 503
    assert refused.value.detail == LOCK_UNAVAILABLE_DETAIL
    assert await _stored(sessions) == before
    assert before.markers == []
    assert scheduled == []
