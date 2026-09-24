from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.crud.user import get_user_by_id, modify_user as crud_modify_user
from app.db.crud.wireguard import get_subnet_usage
from app.db.models import Admin, AdminRole, Base, CoreConfig, Group, ProxyInbound, User
from app.models.core import CoreType
from app.models.group import GroupModify
from app.models.proxy import ProxyTable
from app.models.user import UserModify
from app.operation import OperatorType
from app.operation.group import GroupOperation
from app.operation.user import UserOperation

PREFIX = "a3u-"
WG_TAG = f"{PREFIX}wg"
OVPN = f"{PREFIX}ovpn"
OWNER = SimpleNamespace(username=f"{PREFIX}owner", is_owner=True, role=None, id=None)


def _foreign_keys_on(dbapi_connection, _record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


@pytest_asyncio.fixture
async def engine(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'a3u.db'}")
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


async def _pool_used(sessions):
    async with sessions() as db:
        usage = await get_subnet_usage(db)
    return sum(entry["used"] for entry in usage)


@pytest.mark.asyncio
async def test_a_user_edit_that_overlaps_a_group_change_keeps_what_the_group_change_granted(sessions):
    seed = await _seed(sessions, ["member"])
    member = seed.users["member"]

    async with sessions() as edit_db:
        db_user = await get_user_by_id(edit_db, member, load_admin_role=True)

        async with sessions() as group_db:
            await _groups().modify_group(
                group_db, seed.groups["bare"], GroupModify(name=f"{PREFIX}bare", inbound_tags=[WG_TAG, OVPN]), OWNER
            )
        granted = await _wireguard_of(sessions, member)
        assert granted.peer_ips and granted.openvpn and granted.public_key

        await _users()._modify_user(
            edit_db, db_user, UserModify(data_limit=10 * 1024**3), OWNER, skip_role_limits=True
        )

    after = await _wireguard_of(sessions, member)
    held = await _held_ips(sessions)
    assert after.peer_ips == granted.peer_ips, (
        f"group change gave {granted.peer_ips}, the unrelated edit left {after.peer_ips}"
    )
    assert after.openvpn == granted.openvpn, "the OpenVPN password the group change issued was replaced"
    assert after.public_key == granted.public_key, "the WireGuard key the group change issued was replaced"
    assert await _pool_used(sessions) == len(held), f"pool marks {await _pool_used(sessions)} used, users hold {held}"


@pytest.mark.asyncio
async def test_an_edit_that_sends_its_own_peer_ips_keeps_the_pool_allocation(sessions):
    seed = await _seed(sessions, ["member"])
    member = seed.users["member"]
    async with sessions() as group_db:
        await _groups().modify_group(
            group_db, seed.groups["bare"], GroupModify(name=f"{PREFIX}bare", inbound_tags=[WG_TAG, OVPN]), OWNER
        )
    granted = await _wireguard_of(sessions, member)

    async with sessions() as db:
        db_user = await get_user_by_id(db, member, load_admin_role=True)
        sent = ProxyTable.model_validate(db_user.proxy_settings).dict()
        sent["wireguard"]["peer_ips"] = ["10.78.0.200/32"]
        await crud_modify_user(db, db_user, UserModify(proxy_settings=sent))

    after = await _wireguard_of(sessions, member)
    assert after.peer_ips == granted.peer_ips
    assert await _held_ips(sessions) == {granted.peer_ips[0]: [member]}
