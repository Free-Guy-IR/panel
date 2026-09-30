import asyncio
import os
import sqlite3
import time
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import delete, event, select, text, update
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
MTP = f"{PREFIX}mtp"
SUBNET = "10.77.0.0/24"
OWNER = SimpleNamespace(username=f"{PREFIX}owner", is_owner=True)
BALLAST_ROWS = 10
DEADLOCK_SETUP_SECONDS = 0.3
REPAIR_HOLD_SECONDS = 1.0
BOUNDED_LOCK_WAIT_SECONDS = 10


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
        mtproto_inbound = ProxyInbound(tag=MTP)
        db.add_all([*inbounds.values(), mtproto_inbound])
        await db.flush()
        db.add_all(
            [
                CoreConfig(
                    name=f"{PREFIX}wg",
                    type=CoreType.wg,
                    config={"interface_name": WG_TAG, "address": ["10.77.0.1/24"], "listen_port": 51820},
                ),
                CoreConfig(name=f"{PREFIX}ovpn", type=CoreType.openvpn, config={"instances": [{"tag": OVPN}]}),
                CoreConfig(name=f"{PREFIX}mtp", type=CoreType.mtproto, config={"instances": [{"tag": MTP}]}),
            ]
        )
        groups = {
            "bare": Group(name=f"{PREFIX}bare", inbounds=[]),
            "mtp": Group(name=f"{PREFIX}mtp", inbounds=[mtproto_inbound]),
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
        mtproto=table.mtproto.secret,
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


@pytest_asyncio.fixture
async def repair(monkeypatch, sessions):
    from app.fork.proxy_secrets import group_repair

    state = SimpleNamespace(scheduled=[], tasks=[], synced=[])
    monkeypatch.setattr(group_repair, "_pending_repairs", {})
    monkeypatch.setattr(group_repair, "_repair_owners", {})
    monkeypatch.setattr(group_repair, "_dirty_user_ids", set())

    class _TestGetDB:
        def __init__(self):
            self.db = sessions()

        async def __aenter__(self):
            return self.db

        async def __aexit__(self, exc_type, exc_value, traceback):
            if exc_type is not None:
                await self.db.rollback()
            await self.db.close()

    async def record_sync(users):
        state.synced.append(sorted(user.id for user in users))

    real_schedule = group_repair.schedule_allocation_repair

    def schedule(user_ids):
        state.scheduled.append(sorted(user_ids))
        task = real_schedule(user_ids)
        if task is not None:
            state.tasks.append(task)
        return task

    monkeypatch.setattr(group_repair, "GetDB", _TestGetDB)
    monkeypatch.setattr(group_repair, "sync_users", record_sync)
    monkeypatch.setattr(group_repair, "IN_REQUEST_RETRY_PAUSE_SECONDS", 0.05)
    monkeypatch.setattr(group_repair, "REPAIR_DELAYS_SECONDS", (0.05, 0.05, 0.05))
    monkeypatch.setattr(group_repair, "schedule_allocation_repair", schedule)
    yield state
    await group_repair.cancel_pending_repairs()


def _failing_lock(monkeypatch, failure, *, failures_before_success):
    from app.fork.proxy_secrets import entitled as entitled_module

    real_acquire = entitled_module.acquire_proxy_secrets_lock
    calls = SimpleNamespace(count=0)

    async def acquire(db):
        calls.count += 1
        if failures_before_success is None or calls.count <= failures_before_success:
            raise failure
        return await real_acquire(db)

    monkeypatch.setattr(entitled_module, "acquire_proxy_secrets_lock", acquire)
    return calls


def _in_request_attempts(sessions, failure):
    from app.fork.proxy_secrets.locks import MYSQL_FAMILY, is_deadlock

    return 1 if sessions.kw["bind"].dialect.name in MYSQL_FAMILY and is_deadlock(failure) else 2


def _errors(caplog):
    return [record.getMessage() for record in caplog.records if record.levelname == "ERROR"]


def _warnings(caplog):
    return [record.getMessage() for record in caplog.records if record.levelname == "WARNING"]


@pytest.mark.parametrize("path", ["modify_group", "bulk_add_groups", "bulk_set_groups_disabled"])
@pytest.mark.parametrize("failure", list(FAILURES), ids=list(FAILURES))
@pytest.mark.asyncio
async def test_a_group_change_that_cannot_take_the_secret_lock_twice_answers_503_and_keeps_nothing_of_the_grant_step(
    sessions, monkeypatch, repair, path, failure
):
    seed = await _seed(sessions)
    calls = _failing_lock(monkeypatch, FAILURES[failure], failures_before_success=None)
    _, member, change = _group_change(seed, path)

    async with sessions() as db:
        with pytest.raises(HTTPException) as raised:
            await change(db)
        await db.commit()

    assert raised.value.status_code == 503
    assert "saved" in raised.value.detail
    assert calls.count == _in_request_attempts(sessions, FAILURES[failure])
    assert repair.scheduled == [[seed.users[member]]]
    state = await _phase_two_state(sessions, seed.users[member])
    assert state.peer_ips == []
    assert state.openvpn is None
    assert state.pool_rows == 0


@pytest.mark.parametrize("path", ["modify_group", "bulk_add_groups", "bulk_set_groups_disabled"])
@pytest.mark.asyncio
async def test_a_lock_that_clears_before_the_retry_completes_the_group_change_in_the_request(
    sessions, monkeypatch, repair, path
):
    seed = await _seed(sessions)
    calls = _failing_lock(monkeypatch, FAILURES["mysql-lock-wait-timeout"], failures_before_success=1)
    _, member, change = _group_change(seed, path)

    async with sessions() as db:
        await change(db)

    assert calls.count == 2
    assert repair.scheduled == []
    state = await _phase_two_state(sessions, seed.users[member])
    assert state.peer_ips
    assert state.openvpn
    assert state.pool_rows == 1


@pytest.mark.asyncio
async def test_a_group_change_that_still_fails_is_finished_by_the_background_repair(
    sessions, monkeypatch, repair, caplog
):
    seed = await _seed(sessions)
    member = seed.users["paused_member"]
    calls = _failing_lock(monkeypatch, FAILURES["lock-row-missing"], failures_before_success=3)
    _, _, change = _group_change(seed, "bulk_set_groups_disabled")

    async with sessions() as db:
        with pytest.raises(HTTPException) as raised:
            await change(db)

    assert raised.value.status_code == 503
    assert repair.scheduled == [[member]]
    healed = await repair.tasks[0]

    assert healed is True
    assert calls.count == 4
    assert repair.synced == [[member]]
    state = await _phase_two_state(sessions, member)
    assert state.peer_ips
    assert state.openvpn
    assert state.pool_rows == 1
    assert len([message for message in _warnings(caplog) if "attempt 1/3" in message]) == 1
    assert _errors(caplog) == []


@pytest.mark.asyncio
async def test_a_background_repair_that_never_gets_the_lock_gives_up_with_an_error_and_writes_nothing(
    sessions, monkeypatch, repair, caplog
):
    seed = await _seed(sessions)
    member = seed.users["paused_member"]
    calls = _failing_lock(monkeypatch, FAILURES["deadlock"], failures_before_success=None)
    _, _, change = _group_change(seed, "bulk_set_groups_disabled")

    async with sessions() as db:
        with pytest.raises(HTTPException):
            await change(db)
    healed = await repair.tasks[0]

    assert healed is False
    assert calls.count == _in_request_attempts(sessions, FAILURES["deadlock"]) + 3
    assert repair.synced == []
    state = await _phase_two_state(sessions, member)
    assert state.peer_ips == []
    assert state.openvpn is None
    assert state.pool_rows == 0
    attempt_warnings = [message for message in _warnings(caplog) if "attempt" in message and "/3" in message]
    assert len(attempt_warnings) == 3
    errors = _errors(caplog)
    assert len(errors) == 1
    assert "gave up" in errors[0] and str(member) in errors[0]


async def _fail_the_enable_then_run(sessions, monkeypatch, seed, second_change):
    from app.fork.proxy_secrets import group_repair

    monkeypatch.setattr(group_repair, "REPAIR_DELAYS_SECONDS", (REPAIR_HOLD_SECONDS, 0.05, 0.05))
    _failing_lock(monkeypatch, FAILURES["lock-row-missing"], failures_before_success=2)
    _, _, enable = _group_change(seed, "bulk_set_groups_disabled")
    async with sessions() as db:
        with pytest.raises(HTTPException):
            await enable(db)
    async with sessions() as db:
        await second_change(db)


@pytest.mark.asyncio
async def test_a_member_removed_before_the_repair_runs_ends_with_nothing(sessions, monkeypatch, repair):
    seed = await _seed(sessions)
    member = seed.users["paused_member"]
    operation = _groups()

    await _fail_the_enable_then_run(
        sessions,
        monkeypatch,
        seed,
        lambda db: operation.bulk_remove_groups(
            db, BulkGroup(group_ids={seed.groups["paused"]}, users={member}), OWNER
        ),
    )
    assert await repair.tasks[0] is True

    state = await _phase_two_state(sessions, member)
    assert state.peer_ips == []
    assert state.openvpn is None


@pytest.mark.asyncio
async def test_a_member_healed_by_a_later_change_is_not_given_a_second_address_or_secret(sessions, monkeypatch, repair):
    seed = await _seed(sessions)
    member = seed.users["paused_member"]
    operation = _groups()

    await _fail_the_enable_then_run(
        sessions,
        monkeypatch,
        seed,
        lambda db: operation.modify_group(
            db, seed.groups["paused"], GroupModify(name=f"{PREFIX}paused", inbound_tags=[WG_TAG, OVPN]), OWNER
        ),
    )
    healed_by_the_change = await _phase_two_state(sessions, member)
    assert await repair.tasks[0] is True

    after_the_repair = await _phase_two_state(sessions, member)
    assert len(healed_by_the_change.peer_ips) == 1
    assert healed_by_the_change.openvpn
    assert after_the_repair.peer_ips == healed_by_the_change.peer_ips
    assert after_the_repair.openvpn == healed_by_the_change.openvpn
    assert after_the_repair.pool_rows == 1


@pytest.mark.asyncio
async def test_repairs_for_the_same_users_are_not_multiplied(repair, monkeypatch):
    from app.fork.proxy_secrets import group_repair

    monkeypatch.setattr(group_repair, "REPAIR_DELAYS_SECONDS", (60.0,))
    first = group_repair.schedule_allocation_repair([7, 8])
    second = group_repair.schedule_allocation_repair([8, 7])
    third = group_repair.schedule_allocation_repair([8, 9])

    assert repair.scheduled == [[7, 8], [7, 8], [8, 9]]
    assert first is not None
    assert second is None
    assert third is not None
    assert group_repair.pending_repair_user_ids() == {7, 8, 9}
    assert group_repair.dirty_repair_user_ids() == {7, 8}
    assert group_repair._pending_repairs[third] == [9]


@pytest.mark.asyncio
async def test_a_repair_still_pending_at_shutdown_is_cancelled_with_a_warning(repair, monkeypatch, caplog):
    from app.fork.proxy_secrets import group_repair
    from app.lifecycle import shutdown_functions

    monkeypatch.setattr(group_repair, "REPAIR_DELAYS_SECONDS", (60.0,))
    task = group_repair.schedule_allocation_repair([41])

    await group_repair.cancel_pending_repairs()

    assert task.cancelled()
    assert group_repair.pending_repair_user_ids() == set()
    assert any("shutting down before" in message and "41" in message for message in _warnings(caplog))
    assert group_repair.cancel_pending_repairs in shutdown_functions


def _switchable_lock(monkeypatch):
    from app.fork.proxy_secrets import entitled as entitled_module

    real_acquire = entitled_module.acquire_proxy_secrets_lock
    switch = SimpleNamespace(failing=True)

    async def acquire(db):
        if switch.failing:
            raise FAILURES["lock-row-missing"]
        return await real_acquire(db)

    monkeypatch.setattr(entitled_module, "acquire_proxy_secrets_lock", acquire)
    return switch


@pytest.mark.asyncio
async def test_a_failure_arriving_while_its_users_repair_runs_gets_a_pass_that_reads_the_newer_change(
    sessions, monkeypatch, repair
):
    from app.fork.proxy_secrets import group_repair

    seed = await _seed(sessions)
    member = seed.users["paused_member"]
    operation = _groups()
    switch = _switchable_lock(monkeypatch)
    first_pass_committed = asyncio.Event()
    second_change_failed = asyncio.Event()
    passes = []

    async def gated_sync(users):
        passes.append(sorted(user.id for user in users))
        if len(passes) == 1:
            first_pass_committed.set()
            await second_change_failed.wait()

    monkeypatch.setattr(group_repair, "sync_users", gated_sync)
    _, _, enable = _group_change(seed, "bulk_set_groups_disabled")

    async with sessions() as db:
        with pytest.raises(HTTPException):
            await enable(db)
    switch.failing = False
    await first_pass_committed.wait()

    switch.failing = True
    async with sessions() as db:
        with pytest.raises(HTTPException):
            await operation.bulk_add_groups(db, BulkGroup(group_ids={seed.groups["mtp"]}, users={member}), OWNER)
    switch.failing = False
    second_change_failed.set()

    assert await repair.tasks[0] is True
    assert passes == [[member], [member]]
    state = await _phase_two_state(sessions, member)
    assert len(state.peer_ips) == 1
    assert state.openvpn
    assert state.mtproto
    assert group_repair.pending_repair_user_ids() == set()


@pytest.mark.asyncio
async def test_users_that_keep_changing_stop_the_repair_at_the_pass_bound_with_an_error(
    sessions, monkeypatch, repair, caplog
):
    from app.fork.proxy_secrets import group_repair

    seed = await _seed(sessions)
    member = seed.users["paused_member"]
    switch = _switchable_lock(monkeypatch)
    monkeypatch.setattr(group_repair, "REPAIR_MAX_PASSES", 2)
    passes = []

    async def another_failure_every_pass(users):
        passes.append(sorted(user.id for user in users))
        group_repair.schedule_allocation_repair([member])

    monkeypatch.setattr(group_repair, "sync_users", another_failure_every_pass)
    _, _, enable = _group_change(seed, "bulk_set_groups_disabled")

    async with sessions() as db:
        with pytest.raises(HTTPException):
            await enable(db)
    switch.failing = False

    assert await repair.tasks[0] is False
    assert passes == [[member], [member]]
    errors = _errors(caplog)
    assert len(errors) == 1
    assert "kept changing" in errors[0] and str(member) in errors[0]
    assert group_repair.pending_repair_user_ids() == set()


@pytest.mark.asyncio
async def test_a_group_request_waits_at_most_twice_the_bounded_lock_wait_and_restores_the_connection(
    engine, monkeypatch, repair
):
    from app.fork.proxy_secrets import group_repair
    from app.fork.proxy_secrets.locks import PROXY_SECRETS_LOCK, acquire_named_lock

    if engine.dialect.name not in ("mysql", "mariadb"):
        pytest.skip(reason="the bounded lock wait is a MySQL and MariaDB session variable")
    single = create_async_engine(engine.url, pool_size=1, max_overflow=0)
    victim_sessions = async_sessionmaker(single, class_=AsyncSession, autoflush=False, expire_on_commit=False)
    holder_sessions = async_sessionmaker(engine, class_=AsyncSession, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(group_repair, "REPAIR_DELAYS_SECONDS", (600.0,))
    seed = await _seed(holder_sessions)
    connection_ids = []
    real_acquire = group_repair.acquire_proxy_secrets_lock

    async def remember_the_connection(db):
        connection_ids.append(await db.scalar(text("SELECT CONNECTION_ID()")))
        await real_acquire(db)

    monkeypatch.setattr(group_repair, "acquire_proxy_secrets_lock", remember_the_connection)
    _, _, enable = _group_change(seed, "bulk_set_groups_disabled")

    try:
        async with single.connect() as connection:
            before = await connection.scalar(text("SELECT @@SESSION.innodb_lock_wait_timeout"))
        async with holder_sessions() as holder:
            await acquire_named_lock(holder, PROXY_SECRETS_LOCK)
            async with victim_sessions() as db:
                started = time.monotonic()
                with pytest.raises(HTTPException) as raised:
                    await enable(db)
                waited = time.monotonic() - started
            await holder.rollback()
        async with single.connect() as connection:
            after_id = await connection.scalar(text("SELECT CONNECTION_ID()"))
            after = await connection.scalar(text("SELECT @@SESSION.innodb_lock_wait_timeout"))
    finally:
        await single.dispose()

    assert raised.value.status_code == 503
    bound = BOUNDED_LOCK_WAIT_SECONDS
    assert 2 * bound - 1 <= waited <= 2 * bound + 6, waited
    assert set(connection_ids) == {after_id}
    assert after == before
    assert repair.scheduled == []
    async with holder_sessions() as db:
        assert await db.scalar(select(Group.is_disabled).where(Group.id == seed.groups["paused"])) is True


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
async def test_an_unrelated_database_error_in_a_group_change_is_not_turned_into_503(sessions, monkeypatch, repair):
    seed = await _seed(sessions)
    unrelated = OperationalError("SELECT 1", {}, _DriverError(1146, "Table does not exist"))
    _failing_lock(monkeypatch, unrelated, failures_before_success=None)
    _, _, change = _group_change(seed, "bulk_add_groups")

    async with sessions() as db:
        with pytest.raises(OperationalError):
            await change(db)
    assert repair.scheduled == []


@pytest.mark.asyncio
async def test_a_group_change_waiting_for_the_lock_holds_no_row_its_holder_needs_and_does_its_step_once(
    engine, sessions, monkeypatch, repair
):
    from app.fork.proxy_secrets import group_repair
    from app.fork.proxy_secrets.locks import PROXY_SECRETS_LOCK, acquire_named_lock

    if engine.dialect.name == "sqlite":
        pytest.skip(reason="sqlite serializes writers and has no deadlock victims")
    seed = await _seed(sessions)
    member = seed.users["paused_member"]
    victim_waits_for_the_lock = asyncio.Event()
    real_acquire = group_repair.acquire_proxy_secrets_lock

    async def announce_then_acquire(db):
        victim_waits_for_the_lock.set()
        await real_acquire(db)

    monkeypatch.setattr(group_repair, "acquire_proxy_secrets_lock", announce_then_acquire)
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

    outcome = (await asyncio.gather(waiting, return_exceptions=True))[0]
    assert not isinstance(outcome, BaseException)
    assert repair.scheduled == []

    state = await _phase_two_state(sessions, member)
    assert len(state.peer_ips) == 1
    assert state.openvpn
    assert state.pool_rows == 1
    async with sessions() as db:
        assert await db.scalar(select(User.note).where(User.id == member)) == "holder"
