from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.crud.user import create_user, get_user_by_id, modify_user, revoke_user_sub
from app.db.models import Admin, AdminRole, Base, CoreConfig, Group, ProxyInbound, User
from app.models.core import CoreType
from app.models.proxy import ProxyTable
from app.models.user import UserCreate, UserModify
from app.operation import OperatorType
from app.operation.user import UserOperation
from app.utils.crypto import generate_wireguard_keypair

WG_TAG = "WG_1"
VLESS = "vless-in"
OWNER = SimpleNamespace(username="owner", is_owner=True, role=None)
STORED_PRIVATE, STORED_PUBLIC = generate_wireguard_keypair()
CLIENT_PRIVATE, CLIENT_PUBLIC = generate_wireguard_keypair()


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


@pytest.fixture(autouse=True)
def synced(monkeypatch):
    from app.operation import user as user_module

    calls = []

    async def record(users):
        calls.append(sorted(user.username for user in users))

    monkeypatch.setattr(user_module, "sync_users", record)
    return calls


def _operation():
    operation = UserOperation.__new__(UserOperation)
    operation.operator_type = OperatorType.API
    return operation


def _client_wireguard():
    return {"wireguard": {"private_key": CLIENT_PRIVATE, "public_key": CLIENT_PUBLIC}}


async def _seed(db):
    role = AdminRole(name="owner", is_owner=True)
    db.add(role)
    await db.flush()
    admin = Admin(username="owner", hashed_password="x", role_id=role.id)
    db.add(admin)
    inbounds = {tag: ProxyInbound(tag=tag) for tag in (WG_TAG, VLESS)}
    db.add_all(inbounds.values())
    await db.flush()
    db.add(
        CoreConfig(
            name="wg",
            type=CoreType.wg,
            config={"interface_name": WG_TAG, "address": ["10.0.0.1/24"], "listen_port": 51820},
        )
    )
    groups = {
        "wg": Group(name="wg", inbounds=[inbounds[WG_TAG]]),
        "plain": Group(name="plain", inbounds=[inbounds[VLESS]]),
    }
    db.add_all(groups.values())
    await db.flush()

    settings = ProxyTable().dict()
    settings["wireguard"] = {"private_key": STORED_PRIVATE, "public_key": STORED_PUBLIC, "peer_ips": []}
    legacy = User(username="legacy", proxy_settings=settings, admin_id=admin.id)
    legacy.groups = [groups["plain"]]
    db.add(legacy)
    await db.commit()
    return SimpleNamespace(admin=admin, groups=groups, legacy_id=legacy.id)


async def _load(db, user_id):
    return await get_user_by_id(db, user_id, load_admin_role=True)


async def _stored_wireguard(engine, username):
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as session:
        user = (await session.execute(select(User).where(User.username == username))).scalar_one()
        return user.proxy_settings["wireguard"]


async def _create(db, seed, username, group_name):
    new_user = UserCreate(username=username, proxy_settings=_client_wireguard())
    groups = [seed.groups[group_name]]
    new_user.proxy_settings = await _operation()._prepare_user_proxy_settings(db, groups, new_user.proxy_settings)
    return await create_user(db, new_user, groups, seed.admin)


@pytest.mark.asyncio
async def test_a_created_user_without_wireguard_access_does_not_store_client_keys(engine, db):
    seed = await _seed(db)

    await _create(db, seed, "fresh", "plain")

    stored = await _stored_wireguard(engine, "fresh")
    assert stored.get("private_key") is None
    assert stored.get("public_key") is None


@pytest.mark.asyncio
async def test_a_modified_user_without_wireguard_access_keeps_its_stored_keys(engine, db):
    seed = await _seed(db)
    db_user = await _load(db, seed.legacy_id)
    modified = UserModify(proxy_settings=_client_wireguard())

    await _operation()._prepare_modified_user(db, db_user, modified, OWNER, skip_role_limits=True)
    await modify_user(db, db_user, modified)

    stored = await _stored_wireguard(engine, "legacy")
    assert stored["private_key"] == STORED_PRIVATE
    assert stored["public_key"] == STORED_PUBLIC


@pytest.mark.asyncio
async def test_a_wireguard_entitled_user_keeps_the_unique_keypair_it_sent(engine, db):
    seed = await _seed(db)

    await _create(db, seed, "entitled", "wg")

    stored = await _stored_wireguard(engine, "entitled")
    assert stored["private_key"] == CLIENT_PRIVATE
    assert stored["public_key"] == CLIENT_PUBLIC
    assert stored["peer_ips"]


@pytest.mark.asyncio
async def test_revoking_a_user_without_wireguard_access_keeps_its_stored_keys(engine, db):
    seed = await _seed(db)
    db_user = await _load(db, seed.legacy_id)

    revoked = await _operation()._prepare_revoked_proxy_settings(db, db_user)
    await revoke_user_sub(db, db_user, proxy_settings=revoked.dict())

    stored = await _stored_wireguard(engine, "legacy")
    assert stored["private_key"] == STORED_PRIVATE
    assert stored["public_key"] == STORED_PUBLIC
