import asyncio
import os
import sqlite3
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import delete, event, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.models import (
    Admin,
    AdminRole,
    Base,
    CoreConfig,
    Group,
    ProxyInbound,
    User,
    WireGuardSubnet,
    inbounds_groups_association,
    users_groups_association,
)
from app.fork.proxy_secrets.locks import NamedLockMissing
from app.models.core import CoreType
from app.models.group import BulkGroup, BulkGroupSelection, GroupModify
from app.models.proxy import ProxyTable
from app.operation import OperatorType
from app.operation.group import GroupOperation
from config import database_settings

GITHUB_MODE = os.getenv("TEST_FROM", "local").lower() != "local"
PREFIX = "grplock-"
WG_TAG = f"{PREFIX}wg"
OVPN = f"{PREFIX}ovpn"
SUBNET = "10.77.0.0/24"
OWNER = SimpleNamespace(username=f"{PREFIX}owner", is_owner=True)
BALLAST_ROWS = 10
DEADLOCK_SETUP_SECONDS = 0.3


class _DriverError(Exception):
    def __init__(self, code, message):
        super().__init__(code, message)
        self.sqlstate = None


FAILURES = {
    "lock-row-missing": NamedLockMissing("the 'proxy_secrets' named lock row could not be taken"),
    "deadlock": OperationalError(
        "UPDATE named_locks", {}, _DriverError(1213, "Deadlock found when trying to get lock")
    ),
    "mysql-lock-wait-timeout": OperationalError("UPDATE named_locks", {}, _DriverError(1205, "Lock wait timeout")),
    "sqlite-locked": OperationalError("UPDATE named_locks", {}, sqlite3.OperationalError("database is locked")),
}


def _foreign_keys_on(dbapi_connection, _record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


async def _purge(engine):
    async with engine.begin() as conn:
        user_ids = select(User.id).where(User.username.like(f"{PREFIX}%")).scalar_subquery()
        group_ids = select(Group.id).where(Group.name.like(f"{PREFIX}%")).scalar_subquery()
        await conn.execute(delete(users_groups_association).where(users_groups_association.c.user_id.in_(user_ids)))
        await conn.execute(delete(User.__table__).where(User.username.like(f"{PREFIX}%")))
        await conn.execute(
            delete(inbounds_groups_association).where(inbounds_groups_association.c.group_id.in_(group_ids))
        )
        await conn.execute(delete(Group.__table__).where(Group.name.like(f"{PREFIX}%")))
        await conn.execute(delete(ProxyInbound.__table__).where(ProxyInbound.tag.like(f"{PREFIX}%")))
        await conn.execute(delete(CoreConfig.__table__).where(CoreConfig.name.like(f"{PREFIX}%")))
        await conn.execute(delete(WireGuardSubnet.__table__).where(WireGuardSubnet.network == SUBNET))
        await conn.execute(delete(Admin.__table__).where(Admin.username.like(f"{PREFIX}%")))
        await conn.execute(delete(AdminRole.__table__).where(AdminRole.name.like(f"{PREFIX}%")))


async def _seed_lock_row(engine):
    from app.fork.proxy_secrets.locks import PROXY_SECRETS_LOCK, named_lock_seed_statement

    async with engine.begin() as conn:
        await conn.execute(named_lock_seed_statement(engine.dialect.name, PROXY_SECRETS_LOCK))


@pytest_asyncio.fixture
async def engine(tmp_path):
    if GITHUB_MODE:
        engine = create_async_engine(database_settings.url, poolclass=NullPool)
        await _purge(engine)
        await _seed_lock_row(engine)
        yield engine
        await _purge(engine)
        await engine.dispose()
        return
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'group-lock.db'}")
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
    from app.operation import group as group_module

    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(group_module, "sync_users", nothing)
    for name in ("modify_group", "remove_group"):
        monkeypatch.setattr(notification, name, nothing)


def _groups():
    operation = GroupOperation(operator_type=OperatorType.API)

    async def accept_tags(tags):
        return None

    operation.check_inbound_tags = accept_tags
    return operation


async def _seed(sessions):
    async with sessions() as db:
        role = AdminRole(name=f"{PREFIX}owner", is_owner=True)
        db.add(role)
        await db.flush()
        admin = Admin(username=f"{PREFIX}owner", hashed_password="x", role_id=role.id)
        db.add(admin)
        inbounds = {tag: ProxyInbound(tag=tag) for tag in (WG_TAG, OVPN)}
        db.add_all(inbounds.values())
        await db.flush()
        db.add_all(
            [
                CoreConfig(
                    name=f"{PREFIX}wg",
                    type=CoreType.wg,
                    config={"interface_name": WG_TAG, "address": ["10.77.0.1/24"], "listen_port": 51820},
                ),
                CoreConfig(name=f"{PREFIX}ovpn", type=CoreType.openvpn, config={"instances": [{"tag": OVPN}]}),
            ]
        )
        groups = {
            "bare": Group(name=f"{PREFIX}bare", inbounds=[]),
            "vpn": Group(name=f"{PREFIX}vpn", inbounds=list(inbounds.values())),
            "paused": Group(name=f"{PREFIX}paused", inbounds=list(inbounds.values()), is_disabled=True),
        }
        db.add_all(groups.values())
        await db.flush()
        members = {"bare_member": "bare", "outsider": None, "paused_member": "paused"}
        users = {}
        for label, group_name in members.items():
            user = User(username=f"{PREFIX}{label}", proxy_settings=ProxyTable().dict(), admin_id=admin.id)
            user.groups = [groups[group_name]] if group_name else []
            db.add(user)
            users[label] = user
        ballast = [
            User(username=f"{PREFIX}ballast{index}", proxy_settings=ProxyTable().dict(), admin_id=admin.id)
            for index in range(BALLAST_ROWS)
        ]
        db.add_all(ballast)
        await db.commit()
        return SimpleNamespace(
            groups={name: group.id for name, group in groups.items()},
            users={label: user.id for label, user in users.items()},
            ballast=[user.id for user in ballast],
        )


async def _phase_two_state(sessions, user_id):
    async with sessions() as db:
        settings = await db.scalar(select(User.proxy_settings).where(User.id == user_id))
        pool_rows = (await db.execute(select(WireGuardSubnet.network).where(WireGuardSubnet.network == SUBNET))).all()
    table = ProxyTable.model_validate(settings)
    return SimpleNamespace(
        peer_ips=list(table.wireguard.peer_ips or []),
        openvpn=table.openvpn.password,
        pool_rows=len(pool_rows),
    )


def _group_change(seed, path):
    operation = _groups()
    if path == "modify_group":
        return (
            operation,
            "bare_member",
            lambda db: operation.modify_group(
                db, seed.groups["bare"], GroupModify(name=f"{PREFIX}bare", inbound_tags=[WG_TAG, OVPN]), OWNER
            ),
        )
    if path == "bulk_add_groups":
        return (
            operation,
            "outsider",
            lambda db: operation.bulk_add_groups(
                db, BulkGroup(group_ids={seed.groups["vpn"]}, users={seed.users["outsider"]}), OWNER
            ),
        )
    return (
        operation,
        "paused_member",
        lambda db: operation.bulk_set_groups_disabled(
            db, BulkGroupSelection(ids={seed.groups["paused"]}), OWNER, is_disabled=False
        ),
    )


@pytest.mark.parametrize("path", ["modify_group", "bulk_add_groups", "bulk_set_groups_disabled"])
@pytest.mark.parametrize("failure", list(FAILURES), ids=list(FAILURES))
@pytest.mark.asyncio
async def test_a_group_change_that_cannot_take_the_secret_lock_answers_503_and_keeps_nothing_of_the_grant_step(
    sessions, monkeypatch, path, failure
):
    from app.operation import group as group_module

    seed = await _seed(sessions)

    async def refused_after_writing(db, users, **kwargs):
        await db.flush()
        raise FAILURES[failure]

    monkeypatch.setattr(group_module, "grant_entitled_secrets", refused_after_writing)
    _, member, change = _group_change(seed, path)

    async with sessions() as db:
        with pytest.raises(HTTPException) as raised:
            await change(db)
        await db.commit()

    assert raised.value.status_code == 503
    state = await _phase_two_state(sessions, seed.users[member])
    assert state.peer_ips == []
    assert state.openvpn is None
    assert state.pool_rows == 0


@pytest.mark.parametrize("path", ["modify_group", "bulk_add_groups", "bulk_set_groups_disabled"])
@pytest.mark.asyncio
async def test_without_a_lock_problem_the_grant_step_writes_what_the_failure_cases_must_not(sessions, path):
    seed = await _seed(sessions)
    _, member, change = _group_change(seed, path)

    async with sessions() as db:
        await change(db)

    state = await _phase_two_state(sessions, seed.users[member])
    assert state.peer_ips
    assert state.openvpn
    assert state.pool_rows == 1


@pytest.mark.asyncio
async def test_an_unrelated_database_error_in_a_group_change_is_not_turned_into_503(sessions, monkeypatch):
    from app.operation import group as group_module

    seed = await _seed(sessions)
    unrelated = OperationalError("SELECT 1", {}, _DriverError(1146, "Table does not exist"))

    async def broken(db, users, **kwargs):
        raise unrelated

    monkeypatch.setattr(group_module, "grant_entitled_secrets", broken)
    _, _, change = _group_change(seed, "bulk_add_groups")

    async with sessions() as db:
        with pytest.raises(OperationalError):
            await change(db)


@pytest.mark.asyncio
async def test_a_group_change_chosen_as_deadlock_victim_answers_503_and_keeps_nothing_of_the_grant_step(
    engine, sessions, monkeypatch
):
    from app.fork.proxy_secrets import entitled as entitled_module
    from app.fork.proxy_secrets.locks import PROXY_SECRETS_LOCK, acquire_named_lock

    if engine.dialect.name == "sqlite":
        pytest.skip(reason="sqlite serializes writers and has no deadlock victims")
    seed = await _seed(sessions)
    member = seed.users["paused_member"]
    victim_waits_for_the_lock = asyncio.Event()
    real_acquire = entitled_module.acquire_proxy_secrets_lock

    async def announce_then_acquire(db):
        victim_waits_for_the_lock.set()
        await real_acquire(db)

    monkeypatch.setattr(entitled_module, "acquire_proxy_secrets_lock", announce_then_acquire)
    _, _, change = _group_change(seed, "bulk_set_groups_disabled")

    async def victim():
        async with sessions() as db:
            try:
                return await change(db)
            finally:
                await db.commit()

    async with sessions() as holder:
        await acquire_named_lock(holder, PROXY_SECRETS_LOCK)
        await holder.execute(update(User).where(User.id.in_(seed.ballast)).values(note="ballast"))
        waiting = asyncio.create_task(victim())
        await victim_waits_for_the_lock.wait()
        await asyncio.sleep(DEADLOCK_SETUP_SECONDS)
        await holder.execute(update(User).where(User.id == member).values(note="holder"))
        await holder.commit()

    with pytest.raises(HTTPException) as raised:
        await waiting

    assert raised.value.status_code == 503
    state = await _phase_two_state(sessions, member)
    assert state.peer_ips == []
    assert state.openvpn is None
    assert state.pool_rows == 0
    async with sessions() as db:
        assert await db.scalar(select(User.note).where(User.id == member)) == "holder"
