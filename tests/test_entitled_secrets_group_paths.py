from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Admin, AdminRole, Base, CoreConfig, Group, ProxyInbound, User
from app.models.core import CoreType
from app.models.group import BulkGroup, BulkGroupSelection, GroupModify
from app.models.proxy import ProxyTable
from app.models.user import BulkUserFilter
from app.operation import OperatorType
from app.operation.group import GroupOperation
from app.operation.user import UserOperation

OVPN = "ovpn-udp"
L2TP = "l2tp-de"
MTP = "mtp-443"
VLESS = "vless-in"
OWNER = SimpleNamespace(username="owner", is_owner=True)


def _foreign_keys_on(dbapi_connection, _record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


@pytest_asyncio.fixture
async def engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    event.listen(engine.sync_engine, "connect", _foreign_keys_on)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db(engine):
    async with async_sessionmaker(engine, class_=AsyncSession, autoflush=False, expire_on_commit=False)() as session:
        yield session


@pytest.fixture
def pushed(monkeypatch):
    from app import notification
    from app.operation import group as group_module, user as user_module

    calls = []

    async def record(users):
        calls.append(sorted(user.username for user in users))

    async def no_notification(*args, **kwargs):
        return None

    monkeypatch.setattr(group_module, "sync_users", record)
    monkeypatch.setattr(user_module, "sync_users", record)
    for name in ("modify_group", "remove_group"):
        monkeypatch.setattr(notification, name, no_notification)
    return calls


def _groups():
    operation = GroupOperation(operator_type=OperatorType.API)

    async def accept_tags(tags):
        return None

    operation.check_inbound_tags = accept_tags
    return operation


async def _seed(db):
    role = AdminRole(name="owner", is_owner=True)
    db.add(role)
    await db.flush()
    admin = Admin(username="owner", hashed_password="x", role_id=role.id)
    db.add(admin)
    inbounds = {tag: ProxyInbound(tag=tag) for tag in (OVPN, L2TP, MTP, VLESS)}
    db.add_all(inbounds.values())
    await db.flush()
    db.add_all(
        [
            CoreConfig(name="ovpn", type=CoreType.openvpn, config={"instances": [{"tag": OVPN}]}),
            CoreConfig(
                name="l2tp",
                type=CoreType.l2tp,
                config={"inbound_tag": L2TP, "server_addr": "vpn.example.com", "psk": "correct-horse"},
            ),
            CoreConfig(name="mtproto", type=CoreType.mtproto, config={"instances": [{"tag": MTP}]}),
        ]
    )
    vpn_tags = [inbounds[OVPN], inbounds[L2TP], inbounds[MTP]]
    groups = {
        "vpn": Group(name="vpn", inbounds=list(vpn_tags)),
        "paused": Group(name="paused", inbounds=list(vpn_tags), is_disabled=True),
        "plain": Group(name="plain", inbounds=[inbounds[VLESS]]),
    }
    db.add_all(groups.values())
    await db.flush()

    members = {"plain_member": ["plain"], "paused_member": ["paused"], "bystander": ["plain"]}
    users = {}
    for username, group_names in members.items():
        user = User(username=username, proxy_settings=ProxyTable().dict(), admin_id=admin.id)
        user.groups = [groups[name] for name in group_names]
        db.add(user)
        users[username] = user
    await db.commit()
    return SimpleNamespace(groups=groups, users=users)


async def _secrets(engine):
    async with async_sessionmaker(engine, class_=AsyncSession, autoflush=False, expire_on_commit=False)() as session:
        rows = (await session.execute(select(User))).scalars().all()
        result = {}
        for user in rows:
            table = ProxyTable.model_validate(user.proxy_settings)
            result[user.username] = (table.openvpn.password, table.l2tp.password, table.mtproto.secret)
        return result


def _all_set(values):
    return all(values)


def _none_set(values):
    return values == (None, None, None)


@pytest.mark.asyncio
async def test_enabling_a_paused_group_issues_the_secrets_to_its_members(engine, db, pushed):
    seed = await _seed(db)

    await _groups().bulk_set_groups_disabled(
        db, BulkGroupSelection(ids={seed.groups["paused"].id}), OWNER, is_disabled=False
    )

    stored = await _secrets(engine)
    assert _all_set(stored["paused_member"])
    assert _none_set(stored["plain_member"])
    assert _none_set(stored["bystander"])


@pytest.mark.asyncio
async def test_editing_a_group_that_stays_disabled_issues_nothing(engine, db, pushed):
    seed = await _seed(db)

    await _groups().modify_group(
        db,
        seed.groups["paused"].id,
        GroupModify(name="paused", inbound_tags=[OVPN, L2TP, MTP, VLESS], is_disabled=True),
        OWNER,
    )

    stored = await _secrets(engine)
    assert _none_set(stored["paused_member"])


@pytest.mark.asyncio
async def test_adding_a_disabled_vpn_group_issues_nothing(engine, db, pushed):
    seed = await _seed(db)

    await _groups().bulk_add_groups(
        db, BulkGroup(group_ids={seed.groups["paused"].id}, users={seed.users["plain_member"].id}), OWNER
    )

    stored = await _secrets(engine)
    assert _none_set(stored["plain_member"])


@pytest.mark.asyncio
async def test_removing_a_group_or_deleting_one_issues_nothing(engine, db, pushed):
    seed = await _seed(db)

    await _groups().bulk_remove_groups(
        db, BulkGroup(group_ids={seed.groups["plain"].id}, users={seed.users["bystander"].id}), OWNER
    )
    await _groups().remove_group(db, seed.groups["plain"].id, OWNER)

    stored = await _secrets(engine)
    assert all(_none_set(values) for values in stored.values())


@pytest.mark.asyncio
async def test_only_the_newly_entitled_member_is_changed_and_pushed(engine, db, pushed):
    seed = await _seed(db)

    await _groups().bulk_add_groups(
        db, BulkGroup(group_ids={seed.groups["vpn"].id}, users={seed.users["plain_member"].id}), OWNER
    )

    stored = await _secrets(engine)
    assert _all_set(stored["plain_member"])
    assert _none_set(stored["bystander"])
    assert _none_set(stored["paused_member"])
    assert pushed == [["plain_member"]]


@pytest.mark.asyncio
async def test_a_group_change_keeps_secrets_that_already_exist(engine, db, pushed):
    seed = await _seed(db)
    await _groups().bulk_add_groups(
        db, BulkGroup(group_ids={seed.groups["vpn"].id}, users={seed.users["plain_member"].id}), OWNER
    )
    first = (await _secrets(engine))["plain_member"]

    await _groups().modify_group(
        db, seed.groups["vpn"].id, GroupModify(name="vpn-renamed", inbound_tags=[OVPN, L2TP, MTP]), OWNER
    )

    assert (await _secrets(engine))["plain_member"] == first


@pytest.mark.asyncio
async def test_non_entitled_users_stay_null_after_group_changes_and_after_every_activation(engine, db, pushed):
    seed = await _seed(db)
    await _groups().modify_group(db, seed.groups["plain"].id, GroupModify(name="plain", inbound_tags=[VLESS]), OWNER)
    await _groups().modify_group(
        db,
        seed.groups["paused"].id,
        GroupModify(name="paused", inbound_tags=[OVPN, L2TP, MTP], is_disabled=True),
        OWNER,
    )
    operation = UserOperation.__new__(UserOperation)
    operation.operator_type = OperatorType.API

    await operation.bulk_activate_openvpn_passwords(db, BulkUserFilter())
    await operation.bulk_activate_mtproto_secrets(db, BulkUserFilter())
    await operation.bulk_activate_l2tp_passwords(db, BulkUserFilter())

    stored = await _secrets(engine)
    for username in ("plain_member", "paused_member", "bystander"):
        assert _none_set(stored[username])
