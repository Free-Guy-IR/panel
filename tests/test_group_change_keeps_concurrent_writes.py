from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.crud.user import revoke_user_sub
from app.db.models import (
    Admin,
    AdminRole,
    Base,
    CoreConfig,
    Group,
    ProxyInbound,
    User,
    UserStatus,
)
from app.models.core import CoreType
from app.models.group import GroupModify
from app.models.proxy import ProxyTable
from app.node.user import serialize_users_for_node
from app.operation import OperatorType
from app.operation.group import GroupOperation

PREFIX = "a3-"
WG_TAG = f"{PREFIX}wg"
OVPN = f"{PREFIX}ovpn"
SUBNET = "10.77.0.0/24"
OWNER = SimpleNamespace(username=f"{PREFIX}owner", is_owner=True, role=None)


def _foreign_keys_on(dbapi_connection, _record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


@pytest_asyncio.fixture
async def engine(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'a3.db'}")
    event.listen(engine.sync_engine, "connect", _foreign_keys_on)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def sessions(engine):
    return async_sessionmaker(engine, class_=AsyncSession, autoflush=False, expire_on_commit=False)


@pytest.fixture
def pushed(monkeypatch):
    from app import notification
    from app.fork.proxy_secrets import group_repair
    from app.operation import group as group_module

    log = []

    async def record(users):
        for proto in await serialize_users_for_node(list(users)):
            log.append((int(proto.email), list(proto.inbounds), proto.proxies.vless.id))

    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(group_module, "sync_users", record)
    monkeypatch.setattr(group_repair, "sync_users", record)
    for name in ("modify_group", "remove_group"):
        monkeypatch.setattr(notification, name, nothing)
    return log


def _groups():
    operation = GroupOperation(operator_type=OperatorType.API)

    async def accept_tags(tags):
        return None

    operation.check_inbound_tags = accept_tags
    return operation


async def _seed(sessions, *, member_group="bare"):
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
            "vpn": Group(name=f"{PREFIX}vpn", inbounds=[wg_inbound, ovpn_inbound]),
        }
        db.add_all(groups.values())
        await db.flush()
        user = User(username=f"{PREFIX}member", proxy_settings=ProxyTable().dict(), admin_id=admin.id)
        user.groups = [groups[member_group]]
        db.add(user)
        await db.commit()
        return SimpleNamespace(
            admin_id=admin.id,
            groups={name: group.id for name, group in groups.items()},
            user_id=user.id,
            vless=user.proxy_settings["vless"]["id"],
        )


async def _stored(sessions, user_id):
    async with sessions() as db:
        row = (await db.execute(select(User.status, User.proxy_settings).where(User.id == user_id))).one()
    return row


async def _disable_elsewhere(sessions, user_id):
    async with sessions() as db:
        await db.execute(update(User).where(User.id == user_id).values(status=UserStatus.disabled))
        await db.commit()


async def _revoke_elsewhere(sessions, user_id):
    async with sessions() as db:
        db_user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
        revoked = ProxyTable()
        revoked.wireguard.peer_ips = list(db_user.proxy_settings.get("wireguard", {}).get("peer_ips") or [])
        await revoke_user_sub(db, db_user, proxy_settings=revoked.dict())
        return str(revoked.vless.id)


def _run_before_group_allocation(monkeypatch, action):
    from app.fork.proxy_secrets import group_repair

    real = group_repair.allocate_and_grant_with_one_retry
    state = SimpleNamespace(result=None)

    async def interleaved(db, users, user_ids):
        state.result = await action()
        return await real(db, users, user_ids)

    monkeypatch.setattr(group_repair, "allocate_and_grant_with_one_retry", interleaved)
    return state


def _run_before_repair_allocation(monkeypatch, sessions, action):
    from app.fork.proxy_secrets import group_repair

    class _TestGetDB:
        def __init__(self):
            self.db = sessions()

        async def __aenter__(self):
            return self.db

        async def __aexit__(self, exc_type, exc_value, traceback):
            if exc_type is not None:
                await self.db.rollback()
            await self.db.close()

    real = group_repair.allocate_and_grant
    state = SimpleNamespace(result=None)

    async def interleaved(db, users):
        state.result = await action()
        return await real(db, users)

    monkeypatch.setattr(group_repair, "GetDB", _TestGetDB)
    monkeypatch.setattr(group_repair, "REPAIR_DELAYS_SECONDS", (0.01,))
    monkeypatch.setattr(group_repair, "allocate_and_grant", interleaved)
    monkeypatch.setattr(group_repair, "_pending_repairs", {})
    monkeypatch.setattr(group_repair, "_repair_owners", {})
    monkeypatch.setattr(group_repair, "_dirty_user_ids", set())
    return state


@pytest.mark.asyncio
async def test_a_group_change_does_not_push_a_user_disabled_meanwhile_back_onto_the_nodes(sessions, monkeypatch, pushed):
    seed = await _seed(sessions)
    _run_before_group_allocation(monkeypatch, lambda: _disable_elsewhere(sessions, seed.user_id))

    async with sessions() as db:
        await _groups().modify_group(
            db, seed.groups["bare"], GroupModify(name=f"{PREFIX}bare", inbound_tags=[WG_TAG, OVPN]), OWNER
        )

    status, _ = await _stored(sessions, seed.user_id)
    assert status == UserStatus.disabled
    last_push = [entry for entry in pushed if entry[0] == seed.user_id][-1]
    assert last_push[1] == [], f"disabled user pushed to nodes with inbounds {last_push[1]}"


@pytest.mark.asyncio
async def test_the_background_repair_does_not_push_a_user_disabled_meanwhile_back_onto_the_nodes(
    sessions, monkeypatch, pushed
):
    from app.fork.proxy_secrets import group_repair

    seed = await _seed(sessions, member_group="vpn")
    _run_before_repair_allocation(monkeypatch, sessions, lambda: _disable_elsewhere(sessions, seed.user_id))

    assert await group_repair.repair_allocations([seed.user_id]) is True

    status, _ = await _stored(sessions, seed.user_id)
    assert status == UserStatus.disabled
    last_push = [entry for entry in pushed if entry[0] == seed.user_id][-1]
    assert last_push[1] == [], f"disabled user pushed to nodes with inbounds {last_push[1]}"


@pytest.mark.asyncio
async def test_a_group_change_does_not_undo_a_subscription_revoked_meanwhile(sessions, monkeypatch, pushed):
    seed = await _seed(sessions)
    state = _run_before_group_allocation(monkeypatch, lambda: _revoke_elsewhere(sessions, seed.user_id))

    async with sessions() as db:
        await _groups().modify_group(
            db, seed.groups["bare"], GroupModify(name=f"{PREFIX}bare", inbound_tags=[WG_TAG, OVPN]), OWNER
        )

    _, settings = await _stored(sessions, seed.user_id)
    revoked_vless = state.result
    assert revoked_vless != seed.vless
    assert settings["vless"]["id"] == revoked_vless, (
        f"revoked credential {seed.vless} is back in the database (expected {revoked_vless})"
    )
    last_push = [entry for entry in pushed if entry[0] == seed.user_id][-1]
    assert last_push[2] == revoked_vless, f"nodes were told to accept the revoked credential {last_push[2]}"


@pytest.mark.asyncio
async def test_the_background_repair_does_not_undo_a_subscription_revoked_meanwhile(sessions, monkeypatch, pushed):
    from app.fork.proxy_secrets import group_repair

    seed = await _seed(sessions, member_group="vpn")
    state = _run_before_repair_allocation(monkeypatch, sessions, lambda: _revoke_elsewhere(sessions, seed.user_id))

    assert await group_repair.repair_allocations([seed.user_id]) is True

    _, settings = await _stored(sessions, seed.user_id)
    revoked_vless = state.result
    assert revoked_vless != seed.vless
    assert settings["vless"]["id"] == revoked_vless, (
        f"revoked credential {seed.vless} is back in the database (expected {revoked_vless})"
    )
    last_push = [entry for entry in pushed if entry[0] == seed.user_id][-1]
    assert last_push[2] == revoked_vless, f"nodes were told to accept the revoked credential {last_push[2]}"
