import asyncio
import sqlite3
from types import SimpleNamespace

import anyio
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Admin, AdminRole, Base, CoreConfig, Group, ProxyInbound, User
from app.models.core import CoreType
from app.models.group import GroupModify
from app.models.proxy import ProxyTable
from app.operation import OperatorType
from app.operation.group import GroupOperation

PREFIX = "a3l-"
WG_TAG = f"{PREFIX}wg"
OVPN = f"{PREFIX}ovpn"
OWNER = SimpleNamespace(username=f"{PREFIX}owner", is_owner=True, role=None)


def _foreign_keys_on(dbapi_connection, _record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


@pytest_asyncio.fixture
async def engine(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'a3l.db'}")
    event.listen(engine.sync_engine, "connect", _foreign_keys_on)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def sessions(engine):
    return async_sessionmaker(engine, class_=AsyncSession, autoflush=False, expire_on_commit=False)


@pytest.fixture
def locked_user_rows(engine):
    switch = SimpleNamespace(on=False)

    def refuse(cursor, statement, parameters, context):
        if switch.on and statement.lstrip().upper().startswith("UPDATE USERS"):
            raise sqlite3.OperationalError("database is locked")
        return False

    event.listen(engine.sync_engine, "do_execute", refuse)
    yield switch
    event.remove(engine.sync_engine, "do_execute", refuse)


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    from app import notification
    from app.fork.proxy_secrets import group_repair
    from app.operation import group as group_module

    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(group_module, "sync_users", nothing)
    monkeypatch.setattr(group_repair, "IN_REQUEST_RETRY_PAUSE_SECONDS", 0.01)
    monkeypatch.setattr(group_repair, "schedule_allocation_repair", lambda user_ids: None)
    for name in ("modify_group", "remove_group"):
        monkeypatch.setattr(notification, name, nothing)


async def _seed(sessions, member_group):
    async with sessions() as db:
        role = AdminRole(name=f"{PREFIX}owner", is_owner=True)
        db.add(role)
        await db.flush()
        admin = Admin(username=f"{PREFIX}owner", hashed_password="x", role_id=role.id)
        db.add(admin)
        wg_inbound = ProxyInbound(tag=WG_TAG)
        ovpn_inbound = ProxyInbound(tag=OVPN)
        db.add_all([wg_inbound, ovpn_inbound])
        await db.flush()
        ovpn_core = CoreConfig(name=f"{PREFIX}ovpn", type=CoreType.openvpn, config={"instances": [{"tag": OVPN}]})
        db.add_all(
            [
                CoreConfig(
                    name=f"{PREFIX}wg",
                    type=CoreType.wg,
                    config={"interface_name": WG_TAG, "address": ["10.79.0.1/24"], "listen_port": 51820},
                ),
                ovpn_core,
            ]
        )
        groups = {
            "bare": Group(name=f"{PREFIX}bare", inbounds=[]),
            "vpn": Group(name=f"{PREFIX}vpn", inbounds=[wg_inbound, ovpn_inbound]),
        }
        db.add_all(groups.values())
        await db.flush()
        user = User(username=f"{PREFIX}member", proxy_settings=ProxyTable().dict(), admin_id=admin.id)
        user.groups = [groups[member_group]]
        db.add(user)
        await db.commit()
        return SimpleNamespace(
            groups={name: group.id for name, group in groups.items()},
            user_id=user.id,
            core_id=ovpn_core.id,
            vmess=str(user.proxy_settings["vmess"]["id"]),
            vless=str(user.proxy_settings["vless"]["id"]),
        )


def _messages(caplog):
    return [record.getMessage() for record in caplog.records]


@pytest.mark.asyncio
async def test_a_group_change_that_hits_a_locked_user_row_does_not_log_the_users_credentials(
    sessions, locked_user_rows, caplog
):
    seed = await _seed(sessions, "bare")
    operation = GroupOperation(operator_type=OperatorType.API)

    async def accept_tags(tags):
        return None

    operation.check_inbound_tags = accept_tags
    locked_user_rows.on = True

    async with sessions() as db:
        with pytest.raises(HTTPException) as raised:
            await operation.modify_group(
                db, seed.groups["bare"], GroupModify(name=f"{PREFIX}bare", inbound_tags=[WG_TAG, OVPN]), OWNER
            )

    assert raised.value.status_code == 503
    leaking = [message for message in _messages(caplog) if seed.vmess in message or seed.vless in message]
    assert not leaking, f"user credentials written to the log: {leaking[0][:400]}"


@pytest.mark.asyncio
async def test_a_core_whose_grant_hits_a_locked_user_row_does_not_return_or_log_the_new_password(
    sessions, locked_user_rows, monkeypatch, caplog
):
    from app.fork.operation import entitled_secrets as driver

    seed = await _seed(sessions, "vpn")
    issued = []
    real_issue = driver.issue_unique_secrets

    async def recording_issue(*args, **kwargs):
        values = await real_issue(*args, **kwargs)
        issued.extend(values)
        return values

    monkeypatch.setattr(driver, "issue_unique_secrets", recording_issue)

    class _Operation:
        async def raise_error(self, message, code, db=None):
            raise HTTPException(status_code=code, detail=message)

    locked_user_rows.on = True
    async with sessions() as db:
        core = await db.scalar(select(CoreConfig).where(CoreConfig.id == seed.core_id))
        with pytest.raises(HTTPException) as raised:
            await driver.grant_entitled_secrets_for_core(_Operation(), db, core)

    assert issued
    in_response = [value for value in issued if value in str(raised.value.detail)]
    in_log = [value for value in issued if any(value in message for message in _messages(caplog))]
    assert not in_response, f"new OpenVPN password returned in the API error: {raised.value.detail[:400]}"
    assert not in_log, "new OpenVPN password written to the log"


class _Cursor:
    def __init__(self, server):
        self.server = server
        self.row = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, args=None):
        await asyncio.sleep(0.01)
        if sql.startswith("SELECT"):
            self.row = (self.server.value,)
        else:
            self.server.value = args[0]
            self.row = None

    async def fetchone(self):
        return self.row


class _Server:
    def __init__(self):
        self.value = 50
        self.invalidated = False

    def cursor(self):
        return _Cursor(self)


class _Connection:
    def __init__(self, server):
        self.server = server

    async def get_raw_connection(self):
        return SimpleNamespace(driver_connection=self.server)

    async def invalidate(self):
        self.server.invalidated = True


class _Session:
    def __init__(self, server):
        self.server = server

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="mysql"))

    async def connection(self):
        return _Connection(self.server)


@pytest.mark.asyncio
async def test_a_cancelled_group_request_leaves_its_pooled_connection_restored_or_discarded():
    from app.fork.proxy_secrets.group_repair import _bounded_lock_wait

    server = _Server()
    with anyio.CancelScope() as scope:
        async with _bounded_lock_wait(_Session(server)):
            assert server.value == 10
            scope.cancel()
            await asyncio.sleep(1)

    assert server.value == 50 or server.invalidated, (
        f"connection returned to the pool with innodb_lock_wait_timeout={server.value} and not invalidated"
    )


@pytest.mark.asyncio
async def test_a_request_task_cancelled_inside_the_bounded_wait_leaves_its_connection_restored_or_discarded():
    from app.fork.proxy_secrets.group_repair import _bounded_lock_wait

    server = _Server()
    entered = asyncio.Event()

    async def request():
        async with _bounded_lock_wait(_Session(server)):
            entered.set()
            await asyncio.sleep(1)

    task = asyncio.create_task(request())
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert server.value == 50 or server.invalidated, (
        f"connection returned to the pool with innodb_lock_wait_timeout={server.value} and not invalidated"
    )
