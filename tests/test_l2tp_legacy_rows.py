import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Admin, Base, CoreConfig, Group, ProxyInbound, User
from app.models.core import CoreType
from app.models.protocol import ProxyProtocol
from app.models.proxy import ProxyTable, ProxyTableInput
from app.models.subscription import SubscriptionInboundData, TCPTransportConfig, TLSConfig
from app.models.user import BulkUserFilter, UserCreate, UserModify, UserResponse
from app.node.user import _serialize_user_for_node, safe_l2tp_password
from app.operation import OperatorType
from app.operation.user import UserOperation
from app.subscription.l2tp import L2TPConfiguration

HEALTHY = "Ab3xyzAb3xyzAb3xyzAb"
LEGACY_BAD = [
    'x\nattacker l2tp-de "pw" *',
    "abc",
    "a" * 99,
    "has space here",
    "tab\there",
    "",
    12345,
    b"bytes-password-12345",
]


def _stored(password, vmess_id="b7b0f6ee-3e1b-4a9c-9d8e-4a0e1f2c3d4e"):
    return {"vmess": {"id": vmess_id}, "l2tp": {"password": password}}


@pytest.mark.parametrize("password", LEGACY_BAD)
def test_a_malformed_legacy_row_never_breaks_deserialization(password):
    assert ProxyTable.model_validate(_stored(password)).l2tp.password is None


@pytest.mark.parametrize("password", LEGACY_BAD)
def test_a_malformed_legacy_row_never_breaks_a_user_response(password):
    response = UserResponse.model_validate(
        {
            "id": 7,
            "username": "legacy",
            "status": "active",
            "used_traffic": 0,
            "created_at": "2026-01-01T00:00:00",
            "proxy_settings": _stored(password),
            "group_ids": [],
        }
    )
    assert response.proxy_settings.l2tp.password is None
    assert response.proxy_settings.vmess.id


def test_listing_a_mixed_page_of_healthy_and_legacy_users_succeeds():
    rows = [_stored(HEALTHY), _stored('x\nattacker l2tp-de "pw" *'), _stored("abc"), _stored(None)]
    parsed = [ProxyTable.model_validate(r) for r in rows]
    assert [p.l2tp.password for p in parsed] == [HEALTHY, None, None, None]


@pytest.mark.parametrize("password", LEGACY_BAD)
def test_a_malformed_legacy_row_is_never_shipped_to_a_node(password):
    proto = _serialize_user_for_node(7, _stored(password), ["l2tp-de"], frozenset({ProxyProtocol.l2tp}), None)
    assert proto.proxies.l2tp.password == ""
    assert safe_l2tp_password(password, 7) is None


def test_a_healthy_row_still_reaches_the_node():
    proto = _serialize_user_for_node(7, _stored(HEALTHY), ["l2tp-de"], frozenset({ProxyProtocol.l2tp}), None)
    assert proto.proxies.l2tp.password == HEALTHY


@pytest.mark.parametrize("password", LEGACY_BAD)
def test_a_malformed_legacy_row_is_never_rendered_into_a_subscription(password):
    inbound = SubscriptionInboundData(
        remark="Germany L2TP",
        inbound_tag="l2tp-de",
        protocol="l2tp",
        address="203.0.113.10",
        port=1701,
        network="udp",
        tls_config=TLSConfig(),
        transport_config=TCPTransportConfig(path="", host=[]),
        finalmask={"l2tp": {"server_addr": "vpn.example.com", "psk": "correct-horse-battery"}},
    )
    conf = L2TPConfiguration()
    settings = ProxyTable.model_validate(_stored(password)).l2tp.dict() if hasattr(ProxyTable, "dict") else {}
    conf.add("Germany L2TP", "203.0.113.10", inbound, {"_user_id": 7, "password": settings.get("password")})
    assert conf.details == []


@pytest.mark.parametrize("password", LEGACY_BAD)
def test_admin_input_is_still_rejected_even_though_storage_is_tolerant(password):
    from pydantic import ValidationError

    if password == "":
        assert ProxyTableInput.model_validate({"l2tp": {"password": ""}}).l2tp.password is None
        return
    with pytest.raises(ValidationError):
        UserModify(proxy_settings={"l2tp": {"password": password}})
    with pytest.raises(ValidationError):
        UserCreate(username="legacy", group_ids=[1], data_limit=0, proxy_settings={"l2tp": {"password": password}})


async def _seed_legacy_rows(db):
    admin = Admin(username="owner", hashed_password="x", role_id=1)
    db.add(admin)
    inbound = ProxyInbound(tag="l2tp-de")
    db.add(inbound)
    await db.flush()
    db.add(
        CoreConfig(
            name="L2TP",
            type=CoreType.l2tp,
            config={"inbound_tag": "l2tp-de", "server_addr": "vpn.example.com", "psk": "correct-horse"},
        )
    )
    group = Group(name="l2tp", inbounds=[inbound])
    db.add(group)
    await db.flush()
    vmess_ids = {"poisoned": "b7b0f6ee-3e1b-4a9c-9d8e-4a0e1f2c3d4e", "healthy": "d9d2b8a0-5a3d-4c1e-9f0a-6c2a3b4d5e6f"}
    for username in ("poisoned", "healthy"):
        user = User(username=username, proxy_settings=_stored(HEALTHY, vmess_ids[username]), admin_id=admin.id)
        user.groups = [group]
        db.add(user)
    await db.commit()
    await db.execute(
        update(User).where(User.username == "poisoned").values(proxy_settings=_stored('x\nattacker l2tp-de "pw" *'))
    )
    await db.commit()


@pytest.mark.asyncio
async def test_bulk_activation_repairs_a_legacy_row_and_leaves_healthy_ones_alone(monkeypatch):
    from app.operation import user as user_module

    synced = {}

    async def fake_sync(users):
        synced["usernames"] = [user.username for user in users]

    monkeypatch.setattr(user_module, "sync_users", fake_sync)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as db:
            await _seed_legacy_rows(db)
            operation = UserOperation.__new__(UserOperation)
            operation.operator_type = OperatorType.API

            result = await operation.bulk_activate_l2tp_passwords(db, BulkUserFilter())

            rows = (await db.execute(select(User).execution_options(populate_existing=True))).scalars().all()
            stored = {user.username: user.proxy_settings for user in rows}
    finally:
        await engine.dispose()

    assert result == {"detail": "operation has been successfuly done on 1 users"}
    repaired = stored["poisoned"]["l2tp"]["password"]
    assert repaired != 'x\nattacker l2tp-de "pw" *'
    assert len(repaired) == 20 and repaired.isalnum()
    assert stored["poisoned"]["vmess"]["id"] == "b7b0f6ee-3e1b-4a9c-9d8e-4a0e1f2c3d4e"
    assert stored["healthy"]["l2tp"]["password"] == HEALTHY
    assert synced["usernames"] == ["poisoned"]
