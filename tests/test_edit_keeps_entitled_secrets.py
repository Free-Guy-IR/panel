import base64
from types import SimpleNamespace

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.crud.user import get_user_by_id
from app.db.models import Admin, AdminRole, Base, CoreConfig, Group, ProxyInbound, User
from app.models.core import CoreType
from app.models.proxy import ProxyTable
from app.models.user import UserModify
from app.operation import OperatorType
from app.operation.user import UserOperation

OVPN = "ovpn-udp"
L2TP = "l2tp-de"
MTP = "mtp-443"
PLAIN = "vless-plain"
OWNER = SimpleNamespace(username="owner", is_owner=True, role=None, id=None)
STORED = {
    "openvpn": ("password", "stored-openvpn-password-123"),
    "l2tp": ("password", "StoredL2tpPassword12"),
    "mtproto": ("secret", "0123456789abcdef0123456789abcdef"),
}
STORED_TROJAN = "stored-trojan-password-abc"
NEW_VLESS_ID = "c8c1a7ff-4f2c-4b0d-8e9f-5b1f2a3d4e5f"


@pytest_asyncio.fixture
async def sessions():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, autoflush=False, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def operation(monkeypatch):
    from app import notification
    from app.operation import user as user_module

    async def nothing(*args, **kwargs):
        return None

    async def no_node_push(db_user, include_subscription_url=True):
        return SimpleNamespace(username=db_user.username, status=db_user.status)

    for name in ("modify_user", "user_status_change", "user_subscription_revoked"):
        if hasattr(notification, name):
            monkeypatch.setattr(notification, name, nothing)
    for name in ("sync_user", "sync_users"):
        if hasattr(user_module, name):
            monkeypatch.setattr(user_module, name, nothing)
    op = UserOperation.__new__(UserOperation)
    op.operator_type = OperatorType.API
    op.update_user = no_node_push
    return op


def _wireguard_keypair():
    private = X25519PrivateKey.generate()
    private_b64 = base64.b64encode(private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())).decode()
    public_b64 = base64.b64encode(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    return private_b64, public_b64


async def _seed(sessions, *, entitled=True):
    async with sessions() as db:
        role = AdminRole(name="owner", is_owner=True)
        db.add(role)
        await db.flush()
        admin = Admin(username="owner", hashed_password="x", role_id=role.id)
        entitled_inbounds = [ProxyInbound(tag=tag) for tag in (OVPN, L2TP, MTP)]
        plain_inbound = ProxyInbound(tag=PLAIN)
        db.add_all([admin, *entitled_inbounds, plain_inbound])
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
        vpn_group = Group(name="vpn", inbounds=entitled_inbounds)
        plain_group = Group(name="plain", inbounds=[plain_inbound])
        db.add_all([vpn_group, plain_group])
        await db.flush()
        settings = ProxyTable().dict()
        for protocol, (attribute, value) in STORED.items():
            settings[protocol] = {attribute: value}
        settings["trojan"] = {"password": STORED_TROJAN}
        user = User(username="subject", proxy_settings=settings, admin_id=admin.id)
        user.groups = [vpn_group] if entitled else [plain_group]
        db.add(user)
        await db.commit()
        return SimpleNamespace(user_id=user.id, vpn_group=vpn_group.id, plain_group=plain_group.id)


async def _stored(sessions, user_id):
    async with sessions() as db:
        return (await db.execute(select(User.proxy_settings).where(User.id == user_id))).scalar_one()


async def _modify(operation, sessions, user_id, **fields):
    async with sessions() as db:
        db_user = await get_user_by_id(db, user_id, load_admin_role=True)
        await operation._modify_user(db, db_user, UserModify(**fields), OWNER, skip_role_limits=True)


def _secret(settings, protocol):
    attribute = STORED[protocol][0]
    return (settings.get(protocol) or {}).get(attribute)


@pytest.mark.asyncio
async def test_a_partial_payload_with_only_vless_keeps_every_entitled_secret(sessions, operation):
    seed = await _seed(sessions)

    await _modify(operation, sessions, seed.user_id, proxy_settings={"vless": {"id": NEW_VLESS_ID}})

    stored = await _stored(sessions, seed.user_id)
    assert stored["vless"]["id"] == NEW_VLESS_ID
    assert {protocol: _secret(stored, protocol) for protocol in STORED} == {
        protocol: value for protocol, (_, value) in STORED.items()
    }


@pytest.mark.asyncio
async def test_the_dashboard_regenerate_all_payload_applies_its_new_values(sessions, operation):
    seed = await _seed(sessions)
    private_key, public_key = _wireguard_keypair()
    payload = {
        "vmess": {"id": "11111111-2222-4333-8444-555555555555"},
        "vless": {"id": NEW_VLESS_ID},
        "trojan": {"password": "regenerated-trojan-pass"},
        "shadowsocks": {"password": "regenerated-shadowsocks-password"},
        "hysteria": {"auth": "66666666-7777-4888-9999-000000000000"},
        "wireguard": {"private_key": private_key, "public_key": public_key},
        "mtproto": {"secret": "fedcba9876543210fedcba9876543210"},
        "l2tp": {"password": "RegeneratedL2tpPw99"},
    }

    await _modify(operation, sessions, seed.user_id, proxy_settings=payload)

    stored = await _stored(sessions, seed.user_id)
    assert stored["mtproto"]["secret"] == payload["mtproto"]["secret"]
    assert stored["l2tp"]["password"] == payload["l2tp"]["password"]
    assert stored["trojan"]["password"] == payload["trojan"]["password"]
    assert stored["vless"]["id"] == NEW_VLESS_ID


@pytest.mark.asyncio
async def test_explicit_new_secrets_for_all_three_apply_together(sessions, operation):
    seed = await _seed(sessions)
    explicit = {
        "openvpn": {"password": "explicit-openvpn-password-456"},
        "l2tp": {"password": "ExplicitL2tpPass9876"},
        "mtproto": {"secret": "fedcba9876543210fedcba9876543210"},
    }

    await _modify(operation, sessions, seed.user_id, proxy_settings=explicit)

    stored = await _stored(sessions, seed.user_id)
    assert {protocol: _secret(stored, protocol) for protocol in explicit} == {
        protocol: _secret(explicit, protocol) for protocol in explicit
    }


@pytest.mark.asyncio
async def test_revoke_sub_still_rotates_every_entitled_secret(sessions, operation):
    seed = await _seed(sessions)

    async with sessions() as db:
        db_user = await get_user_by_id(db, seed.user_id, load_admin_role=True)
        await operation._revoke_user_sub(db, db_user, OWNER)

    stored = await _stored(sessions, seed.user_id)
    for protocol, (_, value) in STORED.items():
        assert _secret(stored, protocol), (protocol, stored.get(protocol))
        assert _secret(stored, protocol) != value, protocol


@pytest.mark.asyncio
async def test_a_non_entitled_user_does_not_keep_a_stale_secret_the_payload_omits(sessions, operation):
    seed = await _seed(sessions, entitled=False)

    await _modify(operation, sessions, seed.user_id, proxy_settings={"vless": {"id": NEW_VLESS_ID}})

    stored = await _stored(sessions, seed.user_id)
    kept = {protocol: _secret(stored, protocol) for protocol in STORED if _secret(stored, protocol)}
    assert kept == {}, kept


@pytest.mark.asyncio
async def test_an_edit_that_removes_the_entitling_group_does_not_carry_its_secrets(sessions, operation):
    seed = await _seed(sessions)

    await _modify(
        operation,
        sessions,
        seed.user_id,
        group_ids=[seed.plain_group],
        proxy_settings={"vless": {"id": NEW_VLESS_ID}},
    )

    stored = await _stored(sessions, seed.user_id)
    kept = {protocol: _secret(stored, protocol) for protocol in STORED if _secret(stored, protocol)}
    assert kept == {}, kept


@pytest.mark.asyncio
async def test_a_non_entitled_user_is_never_issued_a_new_secret_by_an_edit(sessions, operation):
    seed = await _seed(sessions, entitled=False)
    async with sessions() as db:
        await db.execute(
            User.__table__.update()
            .where(User.id == seed.user_id)
            .values(proxy_settings=ProxyTable().dict() | {"trojan": {"password": STORED_TROJAN}})
        )
        await db.commit()

    await _modify(operation, sessions, seed.user_id, proxy_settings={"vless": {"id": NEW_VLESS_ID}})

    stored = await _stored(sessions, seed.user_id)
    issued = {protocol: _secret(stored, protocol) for protocol in STORED if _secret(stored, protocol)}
    assert issued == {}, issued
