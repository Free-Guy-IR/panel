from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Admin, AdminRole, Base, CoreConfig, Group, ProxyInbound, User
from app.models.core import CoreType
from app.models.group import BulkGroup, GroupModify
from app.models.proxy import ProxyTable
from app.models.settings import ConfigFormat
from app.models.subscription import SubscriptionInboundData, TCPTransportConfig, TLSConfig
from app.models.user import UsersResponseWithInbounds
from app.operation import OperatorType
from app.operation.group import GroupOperation
from app.operation.subscription import SubscriptionOperation
from app.subscription.config_cache import clear_sub_config_cache

OVPN = "ovpn-udp"
L2TP = "l2tp-de"
MTP = "mtp-443"
VLESS = "vless-in"
CA = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----"
TLS_CRYPT = "-----BEGIN OpenVPN Static key V1-----\nabcd\n-----END OpenVPN Static key V1-----"


def _foreign_keys_on(dbapi_connection, _record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    event.listen(engine.sync_engine, "connect", _foreign_keys_on)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, autoflush=False, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


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
    vpn = Group(name="vpn", inbounds=[inbounds[OVPN], inbounds[L2TP], inbounds[MTP]])
    plain = Group(name="plain", inbounds=[inbounds[VLESS]])
    db.add_all([vpn, plain])
    await db.flush()

    newcomer = User(username="newcomer", proxy_settings=ProxyTable().dict(), admin_id=admin.id)
    newcomer.groups = [plain]
    outsider = User(username="outsider", proxy_settings=ProxyTable().dict(), admin_id=admin.id)
    outsider.groups = [plain]
    db.add_all([newcomer, outsider])
    await db.commit()
    return SimpleNamespace(admin=admin, vpn=vpn, plain=plain, inbounds=inbounds, newcomer=newcomer, outsider=outsider)


async def _stored(db, username):
    db.expire_all()
    user = (await db.execute(select(User).where(User.username == username))).scalar_one()
    return ProxyTable.model_validate(user.proxy_settings)


def _quiet_group_side_effects(monkeypatch):
    from app import notification
    from app.operation import group as group_module

    async def no_sync(users):
        return None

    async def no_notification(*args, **kwargs):
        return None

    monkeypatch.setattr(group_module, "sync_users", no_sync)
    monkeypatch.setattr(notification, "modify_group", no_notification)


def _group_operation():
    operation = GroupOperation(operator_type=OperatorType.API)

    async def accept_tags(tags):
        return None

    operation.check_inbound_tags = accept_tags
    return operation


@pytest.mark.asyncio
async def test_a_user_granted_the_protocols_by_a_bulk_group_add_gets_a_secret_for_each(db, monkeypatch):
    seed = await _seed(db)
    _quiet_group_side_effects(monkeypatch)

    await _group_operation().bulk_add_groups(
        db, BulkGroup(group_ids={seed.vpn.id}, users={seed.newcomer.id}), SimpleNamespace(is_owner=True)
    )

    stored = await _stored(db, "newcomer")
    assert stored.openvpn.password
    assert stored.l2tp.password
    assert stored.mtproto.secret


@pytest.mark.asyncio
async def test_a_user_granted_the_protocols_by_a_group_edit_gets_a_secret_for_each(db, monkeypatch):
    seed = await _seed(db)
    _quiet_group_side_effects(monkeypatch)

    await _group_operation().modify_group(
        db,
        seed.plain.id,
        GroupModify(name="plain", inbound_tags=[VLESS, OVPN, L2TP, MTP]),
        SimpleNamespace(username="owner", is_owner=True),
    )

    for username in ("newcomer", "outsider"):
        stored = await _stored(db, username)
        assert stored.openvpn.password
        assert stored.l2tp.password
        assert stored.mtproto.secret


@pytest.mark.asyncio
async def test_a_group_change_that_grants_nothing_creates_no_secret(db, monkeypatch):
    seed = await _seed(db)
    _quiet_group_side_effects(monkeypatch)

    await _group_operation().modify_group(
        db,
        seed.plain.id,
        GroupModify(name="plain-renamed", inbound_tags=[VLESS]),
        SimpleNamespace(username="owner", is_owner=True),
    )

    stored = await _stored(db, "outsider")
    assert stored.openvpn.password is None
    assert stored.l2tp.password is None
    assert stored.mtproto.secret is None


def _openvpn_host():
    return SubscriptionInboundData(
        remark="Germany OpenVPN",
        inbound_tag=OVPN,
        protocol="openvpn",
        address="203.0.113.10",
        port=[1194],
        network="udp",
        tls_config=TLSConfig(),
        transport_config=TCPTransportConfig(path="", host=[]),
        finalmask={"openvpn": {"ca_cert": CA, "tls_crypt_key": TLS_CRYPT}},
    )


def _subscriber(openvpn_password, inbounds=(OVPN,)):
    settings = ProxyTable().dict()
    settings["openvpn"] = {"password": openvpn_password}
    return UsersResponseWithInbounds.model_validate(
        {
            "id": 41,
            "username": "subscriber",
            "status": "active",
            "used_traffic": 0,
            "created_at": datetime(2026, 1, 1, tzinfo=UTC),
            "proxy_settings": settings,
            "group_ids": [],
            "inbounds": list(inbounds),
        }
    )


@pytest.fixture
def served_hosts(monkeypatch):
    from app.operation import subscription as operation_module
    from app.subscription import share

    hosts = {1: _openvpn_host()}

    async def get_hosts():
        return hosts

    async def settings():
        return SimpleNamespace(randomize_order=False, custom_variables=[])

    async def templates():
        return {"USER_AGENT_TEMPLATE": "", "GRPC_USER_AGENT_TEMPLATE": ""}

    monkeypatch.setattr(share, "host_manager", SimpleNamespace(get_hosts=get_hosts))
    monkeypatch.setattr(share, "subscription_settings", settings)
    monkeypatch.setattr(share, "subscription_client_templates", templates)
    monkeypatch.setattr(operation_module, "subscription_settings", settings)
    clear_sub_config_cache()
    yield hosts
    clear_sub_config_cache()


@pytest.mark.asyncio
async def test_an_entitled_user_with_a_password_gets_a_real_openvpn_profile(served_hosts):
    operation = SubscriptionOperation(operator_type=OperatorType.API)

    body, media_type = await operation.fetch_config(_subscriber("s3cret-pass"), ConfigFormat.openvpn)

    assert media_type == "application/zip"
    assert body


@pytest.mark.asyncio
async def test_an_entitled_user_without_a_password_is_refused_instead_of_served_an_empty_profile(served_hosts):
    operation = SubscriptionOperation(operator_type=OperatorType.API)

    with pytest.raises(HTTPException) as raised:
        await operation.fetch_config(_subscriber(None), ConfigFormat.openvpn)

    assert raised.value.status_code == 409
    assert "OpenVPN" in raised.value.detail
