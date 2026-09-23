import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.crud.wireguard import reconcile_wireguard_subnets
from app.db.models import Admin, AdminRole, Base, CoreConfig, Group, ProxyInbound, User
from app.models.core import CoreType
from app.models.proxy import ProxyTable
from app.utils.crypto import generate_wireguard_keypair, get_wireguard_public_key

WG_TAG = "WG_1"
SHARED_PRIVATE, SHARED_PUBLIC = generate_wireguard_keypair()
OWN_PRIVATE, OWN_PUBLIC = generate_wireguard_keypair()


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


def _settings(private_key, public_key, peer_ips=()):
    settings = ProxyTable().dict()
    settings["wireguard"] = {"private_key": private_key, "public_key": public_key, "peer_ips": list(peer_ips)}
    return settings


async def _seed(db):
    role = AdminRole(name="owner", is_owner=True)
    db.add(role)
    await db.flush()
    admin = Admin(username="owner", hashed_password="x", role_id=role.id)
    db.add(admin)
    inbound = ProxyInbound(tag=WG_TAG)
    db.add(inbound)
    await db.flush()
    db.add(
        CoreConfig(
            name="wg",
            type=CoreType.wg,
            config={"interface_name": WG_TAG, "address": ["10.0.0.1/24"], "listen_port": 51820},
        )
    )
    group = Group(name="wg", inbounds=[inbound])
    db.add(group)
    await db.flush()

    rows = {
        "first_sharer": _settings(SHARED_PRIVATE, SHARED_PUBLIC, ["10.9.9.5/32"]),
        "second_sharer": _settings(SHARED_PRIVATE, SHARED_PUBLIC),
        "own_key": _settings(OWN_PRIVATE, OWN_PUBLIC),
    }
    for username, settings in rows.items():
        user = User(username=username, proxy_settings=settings, admin_id=admin.id)
        user.groups = [group]
        db.add(user)
    await db.commit()


async def _stored(engine):
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as session:
        rows = (await session.execute(select(User))).scalars().all()
        return {user.username: user.proxy_settings["wireguard"] for user in rows}


@pytest.mark.asyncio
async def test_reconcile_gives_every_wireguard_bound_sharer_its_own_key(engine, db):
    await _seed(db)
    before = await _stored(engine)

    changed = await reconcile_wireguard_subnets(db)
    await db.commit()

    after = await _stored(engine)
    assert len(changed) == 3
    for username in ("first_sharer", "second_sharer"):
        assert after[username]["peer_ips"] != before[username]["peer_ips"]
        assert after[username]["public_key"] != SHARED_PUBLIC
        assert after[username]["private_key"] != SHARED_PRIVATE
        assert get_wireguard_public_key(after[username]["private_key"]) == after[username]["public_key"]
    assert after["first_sharer"]["public_key"] != after["second_sharer"]["public_key"]


@pytest.mark.asyncio
async def test_reconcile_leaves_a_unique_complete_keypair_alone(engine, db):
    await _seed(db)

    await reconcile_wireguard_subnets(db)
    await db.commit()

    after = await _stored(engine)
    assert after["own_key"]["peer_ips"]
    assert after["own_key"]["private_key"] == OWN_PRIVATE
    assert after["own_key"]["public_key"] == OWN_PUBLIC
