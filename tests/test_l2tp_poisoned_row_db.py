import json

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.crud.bulk import get_users_for_l2tp_activation
from app.db.models import Admin, Base, CoreConfig, Group, ProxyInbound, User
from app.models.core import CoreType
from app.models.protocol import ProxyProtocol
from app.models.proxy import ProxyTable
from app.models.subscription import SubscriptionInboundData, TCPTransportConfig, TLSConfig
from app.models.user import BulkUserFilter, UserResponse
from app.node.user import _serialize_user_for_node
from app.operation import OperatorType
from app.operation.user import UserOperation
from app.subscription.l2tp import L2TPConfiguration

HEALTHY = "Ab3xyzAb3xyzAb3xyzAb"
POISON = 'x\nattacker l2tp-de "owned" *'
TAG = "l2tp-de"


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


async def _seed(db):
    admin = Admin(username="owner", hashed_password="x", role_id=1)
    db.add(admin)
    await db.flush()

    inbound = ProxyInbound(tag=TAG)
    db.add(inbound)
    await db.flush()

    group = Group(name="l2tp-group", inbounds=[inbound])
    db.add(group)
    await db.flush()

    db.add(
        CoreConfig(
            name="L2TP",
            type=CoreType.l2tp,
            config={"inbound_tag": TAG, "server_addr": "1.2.3.4", "pool": "10.32.0.0/24", "psk": "correct-horse"},
        )
    )
    await db.flush()

    ids = {}
    for name, password in (("healthy", HEALTHY), ("poisoned", HEALTHY), ("fresh", None)):
        settings = ProxyTable().dict()
        settings["l2tp"] = {"password": password}
        user = User(username=name, proxy_settings=settings, admin_id=admin.id)
        user.groups = [group]
        db.add(user)
        await db.flush()
        ids[name] = user.id
    await db.commit()

    await db.execute(
        update(User).where(User.username == "poisoned").values(
            proxy_settings={**ProxyTable().dict(), "l2tp": {"password": POISON}}
        )
    )
    await db.commit()
    return ids


async def _rows(db):
    result = await db.execute(select(User))
    return {u.username: u for u in result.scalars().all()}


@pytest.mark.asyncio
async def test_the_poisoned_row_really_is_in_the_database(db):
    await _seed(db)
    rows = await _rows(db)
    assert rows["poisoned"].proxy_settings["l2tp"]["password"] == POISON


@pytest.mark.asyncio
async def test_listing_every_user_from_the_database_still_works(db):
    await _seed(db)
    rows = await _rows(db)
    responses = [
        UserResponse.model_validate(
            {
                "id": u.id,
                "username": u.username,
                "status": "active",
                "used_traffic": 0,
                "created_at": "2026-01-01T00:00:00",
                "proxy_settings": u.proxy_settings,
                "group_ids": [],
            }
        )
        for u in rows.values()
    ]
    assert len(responses) == 3
    by_name = {r.username: r for r in responses}
    assert by_name["healthy"].proxy_settings.l2tp.password == HEALTHY
    assert by_name["poisoned"].proxy_settings.l2tp.password is None
    assert by_name["fresh"].proxy_settings.l2tp.password is None


@pytest.mark.asyncio
async def test_the_poisoned_row_never_reaches_a_node_from_the_database(db):
    await _seed(db)
    rows = await _rows(db)
    poisoned = _serialize_user_for_node(
        rows["poisoned"].id, rows["poisoned"].proxy_settings, [TAG], frozenset({ProxyProtocol.l2tp}), None
    )
    healthy = _serialize_user_for_node(
        rows["healthy"].id, rows["healthy"].proxy_settings, [TAG], frozenset({ProxyProtocol.l2tp}), None
    )
    assert poisoned.proxies.l2tp.password == ""
    assert healthy.proxies.l2tp.password == HEALTHY


@pytest.mark.asyncio
async def test_the_poisoned_row_renders_no_subscription_entry(db):
    await _seed(db)
    rows = await _rows(db)
    inbound = SubscriptionInboundData(
        remark="Germany L2TP",
        inbound_tag=TAG,
        protocol="l2tp",
        address="203.0.113.10",
        port=1701,
        network="udp",
        tls_config=TLSConfig(),
        transport_config=TCPTransportConfig(path="", host=[]),
        finalmask={"l2tp": {"server_addr": "vpn.example.com", "psk": "correct-horse"}},
    )
    for name, expected in (("poisoned", 0), ("healthy", 1)):
        parsed = ProxyTable.model_validate(rows[name].proxy_settings)
        conf = L2TPConfiguration()
        conf.add("Germany L2TP", "203.0.113.10", inbound, {"_user_id": rows[name].id, "password": parsed.l2tp.password})
        assert len(json.loads(conf.render())) == expected


@pytest.mark.asyncio
async def test_the_bulk_endpoint_reads_the_real_database_and_repairs_the_poisoned_row(db, monkeypatch):
    from app.operation import user as user_module

    ids = await _seed(db)
    synced = {}

    async def fake_sync(users):
        synced["ids"] = [u.id for u in users]

    monkeypatch.setattr(user_module, "sync_users", fake_sync)

    candidates = await get_users_for_l2tp_activation(db, BulkUserFilter())
    assert len(candidates) == 3

    operation = UserOperation.__new__(UserOperation)
    operation.operator_type = OperatorType.API
    result = await operation.bulk_activate_l2tp_passwords(db, BulkUserFilter())

    assert result == {"detail": "operation has been successfuly done on 2 users"}
    assert sorted(synced["ids"]) == sorted([ids["poisoned"], ids["fresh"]])

    rows = await _rows(db)
    repaired = rows["poisoned"].proxy_settings["l2tp"]["password"]
    assert repaired != POISON
    assert len(repaired) == 20 and repaired.isalnum()
    assert rows["healthy"].proxy_settings["l2tp"]["password"] == HEALTHY
    assert rows["poisoned"].proxy_settings["vmess"]["id"]


@pytest.mark.asyncio
async def test_the_dry_run_counts_the_poisoned_row_without_touching_the_database(db):
    await _seed(db)
    operation = UserOperation.__new__(UserOperation)
    operation.operator_type = OperatorType.API

    result = await operation.bulk_activate_l2tp_passwords(db, BulkUserFilter(dry_run=True))

    assert result.affected_users == 2
    rows = await _rows(db)
    assert rows["poisoned"].proxy_settings["l2tp"]["password"] == POISON
