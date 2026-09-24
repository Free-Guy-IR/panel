from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.crud.user import get_user_by_id, modify_user, revoke_user_sub
from app.db.models import Admin, AdminRole, Base, CoreConfig, Group, ProxyInbound, User
from app.models.core import CoreType
from app.models.proxy import ProxyTable
from app.models.user import UserModify
from app.operation import OperatorType
from app.operation.user import UserOperation

OVPN = "ovpn-udp"
L2TP = "l2tp-de"
MTP = "mtp-443"
OWNER = SimpleNamespace(username="owner", is_owner=True, role=None)
STORED = {
    "openvpn": ("password", "stored-openvpn-password-123"),
    "l2tp": ("password", "StoredL2tpPassword12"),
    "mtproto": ("secret", "0123456789abcdef0123456789abcdef"),
}
NEW_VLESS_ID = "c8c1a7ff-4f2c-4b0d-8e9f-5b1f2a3d4e5f"


@pytest_asyncio.fixture
async def sessions():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, autoflush=False, expire_on_commit=False)
    await engine.dispose()


def _operation():
    operation = UserOperation.__new__(UserOperation)
    operation.operator_type = OperatorType.API
    return operation


async def _seed(sessions):
    async with sessions() as db:
        role = AdminRole(name="owner", is_owner=True)
        db.add(role)
        await db.flush()
        admin = Admin(username="owner", hashed_password="x", role_id=role.id)
        inbounds = [ProxyInbound(tag=tag) for tag in (OVPN, L2TP, MTP)]
        db.add_all([admin, *inbounds])
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
        group = Group(name="vpn", inbounds=inbounds)
        db.add(group)
        await db.flush()
        settings = ProxyTable().dict()
        for protocol, (attribute, value) in STORED.items():
            settings[protocol] = {attribute: value}
        user = User(username="entitled", proxy_settings=settings, admin_id=admin.id)
        user.groups = [group]
        db.add(user)
        await db.commit()
        return user.id


async def _stored(sessions, user_id):
    async with sessions() as db:
        return (await db.execute(select(User.proxy_settings).where(User.id == user_id))).scalar_one()


async def _edit(sessions, user_id, proxy_settings):
    async with sessions() as db:
        db_user = await get_user_by_id(db, user_id, load_admin_role=True)
        modified = UserModify(proxy_settings=proxy_settings)
        await _operation()._prepare_modified_user(db, db_user, modified, OWNER, skip_role_limits=True)
        await modify_user(db, db_user, modified)


@pytest.mark.asyncio
async def test_a_user_edit_without_the_openvpn_password_keeps_the_stored_one(sessions):
    user_id = await _seed(sessions)

    await _edit(sessions, user_id, {"vless": {"id": NEW_VLESS_ID}})

    stored = await _stored(sessions, user_id)
    assert stored["openvpn"]["password"] == STORED["openvpn"][1]


@pytest.mark.parametrize("protocol", list(STORED))
@pytest.mark.parametrize(
    "incoming",
    [
        lambda protocol, attribute: {"vless": {"id": NEW_VLESS_ID}},
        lambda protocol, attribute: {"vless": {"id": NEW_VLESS_ID}, protocol: {}},
        lambda protocol, attribute: {"vless": {"id": NEW_VLESS_ID}, protocol: {attribute: None}},
        lambda protocol, attribute: {"vless": {"id": NEW_VLESS_ID}, protocol: {attribute: ""}},
    ],
    ids=["section-absent", "section-empty", "value-null", "value-empty"],
)
@pytest.mark.asyncio
async def test_a_user_edit_that_leaves_an_entitled_secret_empty_keeps_the_stored_one(sessions, protocol, incoming):
    user_id = await _seed(sessions)
    attribute, value = STORED[protocol]

    await _edit(sessions, user_id, incoming(protocol, attribute))

    stored = await _stored(sessions, user_id)
    assert stored[protocol][attribute] == value
    assert stored["vless"]["id"] == NEW_VLESS_ID


@pytest.mark.parametrize(
    ("protocol", "explicit"),
    [
        ("openvpn", "explicit-openvpn-password-456"),
        ("l2tp", "ExplicitL2tpPass9876"),
        ("mtproto", "fedcba9876543210fedcba9876543210"),
    ],
)
@pytest.mark.asyncio
async def test_an_explicit_new_secret_from_the_admin_still_applies(sessions, protocol, explicit):
    user_id = await _seed(sessions)
    attribute, _ = STORED[protocol]

    await _edit(sessions, user_id, {protocol: {attribute: explicit}})

    stored = await _stored(sessions, user_id)
    assert stored[protocol][attribute] == explicit


@pytest.mark.asyncio
async def test_revoking_the_subscription_still_resets_every_entitled_secret(sessions):
    user_id = await _seed(sessions)

    async with sessions() as db:
        db_user = await get_user_by_id(db, user_id, load_admin_role=True)
        revoked = await _operation()._prepare_revoked_proxy_settings(db, db_user)
        await revoke_user_sub(db, db_user, proxy_settings=revoked.dict())

    stored = await _stored(sessions, user_id)
    for protocol, (attribute, value) in STORED.items():
        assert stored[protocol][attribute]
        assert stored[protocol][attribute] != value
