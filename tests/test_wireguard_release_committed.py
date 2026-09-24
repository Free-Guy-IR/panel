from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.crud.user import get_user_by_id
from app.db.models import Admin, AdminRole, Base, CoreConfig, Group, ProxyInbound, User
from app.models.core import CoreType
from app.models.group import BulkGroup
from app.models.proxy import ProxyTable
from app.operation import OperatorType
from app.operation.group import GroupOperation
from app.operation.user import UserOperation

PREFIX = "wgrel-"
WG_TAG = f"{PREFIX}wg"
OVPN = f"{PREFIX}ovpn"
OWNER = SimpleNamespace(username=f"{PREFIX}owner", is_owner=True, role=None, id=None)


def _foreign_keys_on(dbapi_connection, _record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


@pytest_asyncio.fixture
async def engine(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'wgrel.db'}")
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
    from app.operation import group as group_module, user as user_module

    async def nothing(*args, **kwargs):
        return None

    async def no_url(*args, **kwargs):
        return ""

    monkeypatch.setattr(group_module, "sync_users", nothing)
    monkeypatch.setattr(user_module, "sync_user", nothing)
    monkeypatch.setattr(user_module, "sync_remove_users", nothing)
    monkeypatch.setattr(UserOperation, "generate_subscription_url", staticmethod(no_url))
    for name in ("modify_group", "remove_group", "modify_user", "user_status_change", "remove_user"):
        monkeypatch.setattr(notification, name, nothing)


def _groups():
    operation = GroupOperation(operator_type=OperatorType.API)

    async def accept_tags(tags):
        return None

    operation.check_inbound_tags = accept_tags
    return operation


def _users():
    return UserOperation(operator_type=OperatorType.API)


async def _seed(sessions, usernames):
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
                    config={"interface_name": WG_TAG, "address": ["10.78.0.1/24"], "listen_port": 51820},
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
        ids = {}
        for name in usernames:
            user = User(username=f"{PREFIX}{name}", proxy_settings=ProxyTable().dict(), admin_id=admin.id)
            user.groups = [groups["bare"]]
            db.add(user)
            await db.flush()
            ids[name] = user.id
        await db.commit()
        return SimpleNamespace(groups={name: group.id for name, group in groups.items()}, users=ids)


async def _wireguard_of(sessions, user_id):
    async with sessions() as db:
        settings = await db.scalar(select(User.proxy_settings).where(User.id == user_id))
    table = ProxyTable.model_validate(settings)
    return SimpleNamespace(
        peer_ips=list(table.wireguard.peer_ips or []),
        public_key=table.wireguard.public_key,
        openvpn=table.openvpn.password,
    )


async def _held_ips(sessions):
    async with sessions() as db:
        rows = (await db.execute(select(User.id, User.proxy_settings))).all()
    held = {}
    for user_id, settings in rows:
        for ip in ((settings or {}).get("wireguard") or {}).get("peer_ips") or []:
            held.setdefault(ip, []).append(user_id)
    return held


@pytest.mark.asyncio
async def test_deleting_a_user_whose_address_moved_meanwhile_does_not_free_an_address_someone_else_holds(sessions):
    seed = await _seed(sessions, ["leaver", "taker", "late"])
    vpn = seed.groups["vpn"]

    async with sessions() as db:
        await _groups().bulk_add_groups(db, BulkGroup(group_ids={vpn}, users={seed.users["leaver"]}), OWNER)
    leaver_ip = (await _wireguard_of(sessions, seed.users["leaver"])).peer_ips

    async with sessions() as delete_db:
        db_user = await get_user_by_id(delete_db, seed.users["leaver"], load_admin_role=True)

        async with sessions() as db:
            await _groups().bulk_remove_groups(db, BulkGroup(group_ids={vpn}, users={seed.users["leaver"]}), OWNER)
        async with sessions() as db:
            await _groups().bulk_add_groups(db, BulkGroup(group_ids={vpn}, users={seed.users["taker"]}), OWNER)
        assert (await _wireguard_of(sessions, seed.users["taker"])).peer_ips == leaver_ip

        await _users()._remove_user(delete_db, db_user, OWNER)

    async with sessions() as db:
        await _groups().bulk_add_groups(db, BulkGroup(group_ids={vpn}, users={seed.users["late"]}), OWNER)

    held = await _held_ips(sessions)
    shared = {ip: owners for ip, owners in held.items() if len(owners) > 1}
    assert not shared, f"WireGuard addresses handed to two users at once: {shared}"


@pytest.mark.asyncio
async def test_bulk_deleting_users_whose_addresses_moved_meanwhile_does_not_free_addresses_others_hold(sessions):
    from app.db.crud.user import remove_users

    seed = await _seed(sessions, ["leaver", "bystander", "taker", "late"])
    vpn = seed.groups["vpn"]

    async with sessions() as db:
        await _groups().bulk_add_groups(db, BulkGroup(group_ids={vpn}, users={seed.users["leaver"]}), OWNER)
    leaver_ip = (await _wireguard_of(sessions, seed.users["leaver"])).peer_ips

    async with sessions() as delete_db:
        stale_users = [
            await get_user_by_id(delete_db, seed.users[name], load_admin_role=True) for name in ("leaver", "bystander")
        ]

        async with sessions() as db:
            await _groups().bulk_remove_groups(db, BulkGroup(group_ids={vpn}, users={seed.users["leaver"]}), OWNER)
        async with sessions() as db:
            await _groups().bulk_add_groups(db, BulkGroup(group_ids={vpn}, users={seed.users["taker"]}), OWNER)
        assert (await _wireguard_of(sessions, seed.users["taker"])).peer_ips == leaver_ip

        removed_settings = []

        async def record_chunk(chunk):
            removed_settings.extend(
                list(((user.proxy_settings or {}).get("wireguard") or {}).get("peer_ips") or []) for user in chunk
            )

        await remove_users(delete_db, stale_users, on_chunk_deleted=record_chunk)

    async with sessions() as db:
        await _groups().bulk_add_groups(db, BulkGroup(group_ids={vpn}, users={seed.users["late"]}), OWNER)

    held = await _held_ips(sessions)
    shared = {ip: owners for ip, owners in held.items() if len(owners) > 1}
    assert not shared, f"WireGuard addresses handed to two users at once: {shared}"
    assert removed_settings == [[], []]
