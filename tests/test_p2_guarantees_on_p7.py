import asyncio
import time
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.crud.user import get_user_by_id
from app.db.models import Base, User
from app.fork.models.pending_user_resync import PendingUserResync
from app.models.group import BulkGroup, BulkGroupSelection
from app.models.proxy import ProxyTable
from app.models.user import UserModify
from app.operation import OperatorType
from app.operation.user import UserOperation
from app.utils.crypto import generate_wireguard_keypair
from config import database_settings
from tests.test_group_secret_lock_503 import (
    FAILURES,
    GITHUB_MODE,
    OWNER,
    _errors,
    _foreign_keys_on,
    _group_change,
    _groups,
    _phase_two_state,
    _purge,
    _seed,
    _seed_lock_row,
)

EDITED_NOTE = "edited by the user request"
MYSQL_FAMILY = ("mysql", "mariadb")


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
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'guarantees.db'}")
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
    for name in ("modify_group", "remove_group", "modify_user", "user_status_change"):
        monkeypatch.setattr(notification, name, nothing)


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


def _switchable_lock(monkeypatch):
    from app.fork.proxy_secrets import entitled as entitled_module, group_repair

    switch = SimpleNamespace(failing=True)
    for module in (entitled_module, group_repair):
        real_acquire = module.acquire_proxy_secrets_lock

        async def acquire(db, real_acquire=real_acquire):
            if switch.failing:
                raise FAILURES["lock-row-missing"]
            return await real_acquire(db)

        monkeypatch.setattr(module, "acquire_proxy_secrets_lock", acquire)
    return switch


def _users_operation(monkeypatch):
    from app.operation import user as user_module

    async def nothing(*args, **kwargs):
        return None

    async def no_node_push(db_user, include_subscription_url=True):
        return SimpleNamespace(username=db_user.username, status=db_user.status)

    monkeypatch.setattr(user_module, "sync_user", nothing)
    monkeypatch.setattr(user_module, "sync_users", nothing)
    operation = UserOperation.__new__(UserOperation)
    operation.operator_type = OperatorType.API
    operation.update_user = no_node_push
    return operation


async def _markers(sessions, user_id):
    table = PendingUserResync.__table__
    async with sessions() as db:
        return (await db.execute(select(table.c.user_id).where(table.c.user_id == user_id))).scalars().all()


async def _stored_user(sessions, user_id):
    async with sessions() as db:
        user = await db.get(User, user_id)
        return SimpleNamespace(note=user.note, vless_id=(user.proxy_settings.get("vless") or {}).get("id"))


def _vless_id_for(user_id):
    return f"c8c1a7ff-4f2c-4b0d-8e9f-{user_id:012d}"


def _edit(user_id):
    return UserModify(note=EDITED_NOTE, proxy_settings={"vless": {"id": _vless_id_for(user_id)}})


async def _user_edit(operation, sessions, user_id):
    async with sessions() as db:
        db_user = await get_user_by_id(db, user_id, load_admin_role=True)
        await operation._modify_user(db, db_user, _edit(user_id), OWNER, skip_role_limits=True)


@pytest.mark.asyncio
async def test_a_second_failure_before_the_first_pass_reads_is_covered_and_leaves_no_marker(
    sessions, monkeypatch, repair
):
    from app.fork.proxy_secrets import group_repair

    seed = await _seed(sessions)
    member = seed.users["paused_member"]
    operation = _groups()
    switch = _switchable_lock(monkeypatch)
    monkeypatch.setattr(group_repair, "REPAIR_DELAYS_SECONDS", (0.5, 0.05, 0.05))
    _, _, enable = _group_change(seed, "bulk_set_groups_disabled")

    async with sessions() as db:
        with pytest.raises(HTTPException):
            await enable(db)
    async with sessions() as db:
        with pytest.raises(HTTPException):
            await operation.bulk_add_groups(db, BulkGroup(group_ids={seed.groups["mtp"]}, users={member}), OWNER)
    switch.failing = False

    assert await repair.tasks[0] is True
    assert len(repair.tasks) == 1
    state = await _phase_two_state(sessions, member)
    assert len(state.peer_ips) == 1
    assert state.openvpn
    assert state.mtproto
    assert await _markers(sessions, member) == []
    assert group_repair.pending_repair_user_ids() == set()


@pytest.mark.asyncio
async def test_failures_arriving_during_the_first_two_passes_get_a_third_pass(sessions, monkeypatch, repair):
    from app.fork.proxy_secrets import group_repair

    seed = await _seed(sessions)
    member = seed.users["paused_member"]
    operation = _groups()
    switch = _switchable_lock(monkeypatch)
    passes = []
    changes = [
        lambda db: operation.bulk_add_groups(db, BulkGroup(group_ids={seed.groups["mtp"]}, users={member}), OWNER),
        lambda db: operation.bulk_add_groups(db, BulkGroup(group_ids={seed.groups["vpn"]}, users={member}), OWNER),
    ]

    async def change_again_during_the_pass(users):
        passes.append(sorted(user.id for user in users))
        if len(passes) <= len(changes):
            switch.failing = True
            async with sessions() as db:
                with pytest.raises(HTTPException):
                    await changes[len(passes) - 1](db)
            switch.failing = False

    monkeypatch.setattr(group_repair, "sync_users", change_again_during_the_pass)
    _, _, enable = _group_change(seed, "bulk_set_groups_disabled")

    async with sessions() as db:
        with pytest.raises(HTTPException):
            await enable(db)
    switch.failing = False

    assert await repair.tasks[0] is True
    assert passes == [[member], [member], [member]]
    state = await _phase_two_state(sessions, member)
    assert len(state.peer_ips) == 1
    assert state.openvpn
    assert state.mtproto
    assert group_repair.pending_repair_user_ids() == set()


@pytest.mark.asyncio
async def test_a_user_that_keeps_changing_is_stopped_at_five_passes_with_one_error(
    sessions, monkeypatch, repair, caplog
):
    from app.fork.proxy_secrets import group_repair

    seed = await _seed(sessions)
    member = seed.users["paused_member"]
    switch = _switchable_lock(monkeypatch)
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

    assert group_repair.REPAIR_MAX_PASSES == 5
    assert await repair.tasks[0] is False
    assert passes == [[member]] * 5
    errors = _errors(caplog)
    assert len(errors) == 1
    assert "kept changing" in errors[0] and str(member) in errors[0]
    assert group_repair.pending_repair_user_ids() == set()


@pytest.mark.asyncio
async def test_a_user_edit_that_read_the_user_before_a_group_change_keeps_the_group_change(
    sessions, monkeypatch, repair
):
    seed = await _seed(sessions)
    member = seed.users["paused_member"]
    users = _users_operation(monkeypatch)
    _, _, enable = _group_change(seed, "bulk_set_groups_disabled")

    async with sessions() as edit_session:
        stale = await get_user_by_id(edit_session, member, load_admin_role=True)
        async with sessions() as db:
            await enable(db)
        granted = await _phase_two_state(sessions, member)
        await users._modify_user(edit_session, stale, _edit(member), OWNER, skip_role_limits=True)

    state = await _phase_two_state(sessions, member)
    edited = await _stored_user(sessions, member)
    assert edited.note == EDITED_NOTE
    assert edited.vless_id == _vless_id_for(member)
    assert len(granted.peer_ips) == 1
    assert state.peer_ips == granted.peer_ips
    assert state.openvpn == granted.openvpn


@pytest.mark.asyncio
async def test_a_group_change_that_read_the_user_before_a_user_edit_keeps_the_edit(sessions, monkeypatch, repair):
    from app.fork.proxy_secrets import group_repair

    seed = await _seed(sessions)
    member = seed.users["paused_member"]
    users = _users_operation(monkeypatch)
    real_acquire = group_repair.acquire_proxy_secrets_lock
    edited_first = SimpleNamespace(done=False)

    async def let_the_user_edit_commit_first(db):
        if not edited_first.done:
            edited_first.done = True
            await _user_edit(users, sessions, member)
        await real_acquire(db)

    monkeypatch.setattr(group_repair, "acquire_proxy_secrets_lock", let_the_user_edit_commit_first)
    _, _, enable = _group_change(seed, "bulk_set_groups_disabled")

    async with sessions() as db:
        await enable(db)

    assert edited_first.done is True
    state = await _phase_two_state(sessions, member)
    edited = await _stored_user(sessions, member)
    assert edited.note == EDITED_NOTE
    assert edited.vless_id == _vless_id_for(member)
    assert len(state.peer_ips) == 1
    assert state.openvpn


@pytest.mark.asyncio
async def test_concurrent_group_changes_and_user_edits_finish_without_deadlock_or_lost_change(
    engine, sessions, monkeypatch, repair
):
    seed = await _seed(sessions)
    member = seed.users["paused_member"]
    outsider = seed.users["outsider"]
    groups = _groups()
    users = _users_operation(monkeypatch)
    _, _, enable = _group_change(seed, "bulk_set_groups_disabled")

    async def in_session(work):
        async with sessions() as db:
            return await work(db)

    async def edit(user_id):
        await _user_edit(users, sessions, user_id)

    rounds = 1 if engine.dialect.name == "sqlite" else 3
    for _ in range(rounds):
        outcomes = await asyncio.wait_for(
            asyncio.gather(
                in_session(enable),
                in_session(
                    lambda db: groups.bulk_add_groups(
                        db, BulkGroup(group_ids={seed.groups["mtp"]}, users={member, outsider}), OWNER
                    )
                ),
                in_session(
                    lambda db: groups.bulk_add_groups(
                        db, BulkGroup(group_ids={seed.groups["vpn"]}, users={outsider}), OWNER
                    )
                ),
                edit(member),
                edit(outsider),
                return_exceptions=True,
            ),
            timeout=120,
        )
        unexpected = [
            f"{type(outcome).__name__}: {outcome}"
            for outcome in outcomes
            if isinstance(outcome, BaseException)
            and not (isinstance(outcome, HTTPException) and outcome.status_code == 503)
        ]
        assert unexpected == []
    for task in list(repair.tasks):
        assert await asyncio.wait_for(task, timeout=60) is True

    for user_id in (member, outsider):
        state = await _phase_two_state(sessions, user_id)
        edited = await _stored_user(sessions, user_id)
        assert len(state.peer_ips) == 1, (user_id, state)
        assert state.openvpn, (user_id, state)
        assert state.mtproto, (user_id, state)
        assert edited.note == EDITED_NOTE
        assert edited.vless_id == _vless_id_for(user_id)


@pytest.mark.parametrize("release_after", [5.0, 14.0], ids=["within-the-first-wait", "between-the-waits"])
@pytest.mark.asyncio
async def test_a_lock_released_inside_the_bounded_waits_completes_the_request_and_restores_the_connection(
    engine, monkeypatch, repair, release_after
):
    from app.fork.proxy_secrets import group_repair
    from app.fork.proxy_secrets.locks import PROXY_SECRETS_LOCK, acquire_named_lock

    if engine.dialect.name not in MYSQL_FAMILY:
        pytest.skip(reason="the bounded lock wait is a MySQL and MariaDB session variable")
    monkeypatch.setattr(group_repair, "IN_REQUEST_RETRY_PAUSE_SECONDS", 1.0)
    single = create_async_engine(engine.url, pool_size=1, max_overflow=0)
    victim_sessions = async_sessionmaker(single, class_=AsyncSession, autoflush=False, expire_on_commit=False)
    holder_sessions = async_sessionmaker(engine, class_=AsyncSession, autoflush=False, expire_on_commit=False)
    seed = await _seed(holder_sessions)
    _, member, enable = _group_change(seed, "bulk_set_groups_disabled")

    try:
        async with single.connect() as connection:
            before_id = await connection.scalar(text("SELECT CONNECTION_ID()"))
            before = await connection.scalar(text("SELECT @@SESSION.innodb_lock_wait_timeout"))
        async with holder_sessions() as holder:
            await acquire_named_lock(holder, PROXY_SECRETS_LOCK)

            async def victim():
                async with victim_sessions() as db:
                    await enable(db)

            started = time.monotonic()
            waiting = asyncio.create_task(victim())
            await asyncio.sleep(release_after)
            await holder.rollback()
        await waiting
        waited = time.monotonic() - started
        async with single.connect() as connection:
            after_id = await connection.scalar(text("SELECT CONNECTION_ID()"))
            after = await connection.scalar(text("SELECT @@SESSION.innodb_lock_wait_timeout"))
    finally:
        await single.dispose()

    assert waited < 2 * group_repair.IN_REQUEST_LOCK_WAIT_SECONDS + 3, waited
    assert after_id == before_id
    assert after == before
    assert repair.scheduled == []
    state = await _phase_two_state(holder_sessions, seed.users[member])
    assert len(state.peer_ips) == 1
    assert state.openvpn


@pytest.mark.asyncio
async def test_a_request_cancelled_during_the_bounded_wait_still_restores_the_connection(engine, monkeypatch, repair):
    from app.fork.proxy_secrets.locks import PROXY_SECRETS_LOCK, acquire_named_lock

    if engine.dialect.name not in MYSQL_FAMILY:
        pytest.skip(reason="the bounded lock wait is a MySQL and MariaDB session variable")
    single = create_async_engine(engine.url, pool_size=1, max_overflow=0)
    victim_sessions = async_sessionmaker(single, class_=AsyncSession, autoflush=False, expire_on_commit=False)
    holder_sessions = async_sessionmaker(engine, class_=AsyncSession, autoflush=False, expire_on_commit=False)
    seed = await _seed(holder_sessions)
    _, _, enable = _group_change(seed, "bulk_set_groups_disabled")

    try:
        async with single.connect() as connection:
            before = await connection.scalar(text("SELECT @@SESSION.innodb_lock_wait_timeout"))
        async with holder_sessions() as holder:
            await acquire_named_lock(holder, PROXY_SECRETS_LOCK)

            async def victim():
                async with victim_sessions() as db:
                    await enable(db)

            waiting = asyncio.create_task(victim())
            await asyncio.sleep(3)
            waiting.cancel()
            outcome = (await asyncio.gather(waiting, return_exceptions=True))[0]
            await holder.rollback()
        async with single.connect() as connection:
            after = await connection.scalar(text("SELECT @@SESSION.innodb_lock_wait_timeout"))
    finally:
        await single.dispose()

    assert isinstance(outcome, asyncio.CancelledError)
    assert after == before


@pytest.mark.asyncio
async def test_a_user_edit_without_the_openvpn_password_keeps_the_stored_one(sessions, monkeypatch, repair):
    seed = await _seed(sessions)
    member = seed.users["paused_member"]
    users = _users_operation(monkeypatch)
    _, _, enable = _group_change(seed, "bulk_set_groups_disabled")
    async with sessions() as db:
        await enable(db)
    granted = await _phase_two_state(sessions, member)

    await _user_edit(users, sessions, member)

    state = await _phase_two_state(sessions, member)
    assert granted.openvpn
    assert state.openvpn == granted.openvpn
    assert state.peer_ips == granted.peer_ips


async def _wireguard_and_openvpn(sessions, user_id):
    async with sessions() as db:
        settings = await db.scalar(select(User.proxy_settings).where(User.id == user_id))
    table = ProxyTable.model_validate(settings)
    return SimpleNamespace(
        private_key=table.wireguard.private_key,
        public_key=table.wireguard.public_key,
        peer_ips=list(table.wireguard.peer_ips or []),
        openvpn=table.openvpn.password,
    )


@pytest.mark.asyncio
async def test_a_partial_edit_keeps_the_wireguard_keypair_of_an_entitled_user(sessions, monkeypatch, repair):
    seed = await _seed(sessions)
    member = seed.users["paused_member"]
    users = _users_operation(monkeypatch)
    _, _, enable = _group_change(seed, "bulk_set_groups_disabled")
    async with sessions() as db:
        await enable(db)
    granted = await _wireguard_and_openvpn(sessions, member)

    await _user_edit(users, sessions, member)

    after = await _wireguard_and_openvpn(sessions, member)
    assert granted.private_key and granted.openvpn and granted.peer_ips
    assert (after.private_key, after.public_key) == (granted.private_key, granted.public_key)
    assert after.peer_ips == granted.peer_ips
    assert after.openvpn == granted.openvpn


@pytest.mark.asyncio
async def test_a_stale_edit_after_a_grant_keeps_the_granted_wireguard_keypair(sessions, monkeypatch, repair):
    seed = await _seed(sessions)
    member = seed.users["paused_member"]
    users = _users_operation(monkeypatch)
    _, _, enable = _group_change(seed, "bulk_set_groups_disabled")

    async with sessions() as edit_session:
        stale = await get_user_by_id(edit_session, member, load_admin_role=True)
        async with sessions() as db:
            await enable(db)
        granted = await _wireguard_and_openvpn(sessions, member)
        await users._modify_user(edit_session, stale, _edit(member), OWNER, skip_role_limits=True)

    after = await _wireguard_and_openvpn(sessions, member)
    assert granted.private_key and granted.openvpn
    assert (after.private_key, after.public_key) == (granted.private_key, granted.public_key)
    assert after.peer_ips == granted.peer_ips
    assert after.openvpn == granted.openvpn


@pytest.mark.asyncio
async def test_an_explicit_new_wireguard_keypair_still_applies(sessions, monkeypatch, repair):
    seed = await _seed(sessions)
    member = seed.users["paused_member"]
    users = _users_operation(monkeypatch)
    _, _, enable = _group_change(seed, "bulk_set_groups_disabled")
    async with sessions() as db:
        await enable(db)
    granted = await _wireguard_and_openvpn(sessions, member)
    private_key, public_key = generate_wireguard_keypair()

    async with sessions() as db:
        db_user = await get_user_by_id(db, member, load_admin_role=True)
        modified = UserModify(proxy_settings={"wireguard": {"private_key": private_key, "public_key": public_key}})
        await users._modify_user(db, db_user, modified, OWNER, skip_role_limits=True)

    after = await _wireguard_and_openvpn(sessions, member)
    assert (after.private_key, after.public_key) == (private_key, public_key)
    assert after.peer_ips == granted.peer_ips


@pytest.mark.asyncio
async def test_a_partial_edit_does_not_keep_a_wireguard_key_another_user_holds(sessions, monkeypatch, repair):
    seed = await _seed(sessions)
    member = seed.users["paused_member"]
    users = _users_operation(monkeypatch)
    _, _, enable = _group_change(seed, "bulk_set_groups_disabled")
    async with sessions() as db:
        await enable(db)
    granted = await _wireguard_and_openvpn(sessions, member)
    async with sessions() as db:
        sharer = await db.get(User, seed.users["outsider"])
        sharer.proxy_settings = {
            **sharer.proxy_settings,
            "wireguard": {"private_key": granted.private_key, "public_key": granted.public_key, "peer_ips": []},
        }
        await db.commit()

    await _user_edit(users, sessions, member)

    after = await _wireguard_and_openvpn(sessions, member)
    assert after.private_key and after.public_key
    assert after.public_key != granted.public_key
    assert after.peer_ips == granted.peer_ips


@pytest.mark.asyncio
async def test_a_group_disabled_between_the_edits_read_and_its_lock_stops_the_carry_over(sessions, monkeypatch, repair):
    seed = await _seed(sessions)
    member = seed.users["paused_member"]
    users = _users_operation(monkeypatch)
    operation = _groups()
    _, _, enable = _group_change(seed, "bulk_set_groups_disabled")
    async with sessions() as db:
        await enable(db)
    granted = await _wireguard_and_openvpn(sessions, member)

    async with sessions() as edit_session:
        stale = await get_user_by_id(edit_session, member, load_admin_role=True)
        async with sessions() as db:
            await operation.bulk_set_groups_disabled(
                db, BulkGroupSelection(ids={seed.groups["paused"]}), OWNER, is_disabled=True
            )
        await users._modify_user(edit_session, stale, _edit(member), OWNER, skip_role_limits=True)

    after = await _wireguard_and_openvpn(sessions, member)
    assert granted.openvpn
    assert after.openvpn is None
