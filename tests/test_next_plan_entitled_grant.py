import logging
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from app.db.crud.user import get_user_by_id, reset_user_by_next
from app.db.models import Admin, AdminRole, Base, CoreConfig, Group, NextPlan, ProxyInbound, User, UserTemplate
from app.fork.proxy_secrets import ProxySecretUniquenessError, entitled as entitled_module
from app.fork.proxy_secrets.entitled import EntitledSecretField
from app.models.core import CoreType
from app.models.proxy import ProxyTable

OVPN = "ovpn-udp"
L2TP = "l2tp-de"
MTP = "mtp-443"
VLESS = "vless-in"
GIGABYTE = 1024**3


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
def errors_seen():
    records = []
    handler = logging.Handler(level=logging.ERROR)
    handler.emit = records.append
    logger = logging.getLogger("entitled-secrets")
    logger.addHandler(handler)
    yield records
    logger.removeHandler(handler)


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
    plain = Group(name="plain", inbounds=[inbounds[VLESS]])
    vpn = Group(name="vpn", inbounds=[inbounds[OVPN], inbounds[L2TP], inbounds[MTP], inbounds[VLESS]])
    db.add_all([plain, vpn])
    await db.flush()
    template = UserTemplate(
        name="vpn-plan",
        username_prefix=None,
        username_suffix=None,
        extra_settings=None,
        groups=[vpn],
        data_limit=5 * GIGABYTE,
        expire_duration=0,
    )
    db.add(template)
    await db.flush()

    user = User(username="renewing", proxy_settings=ProxyTable().dict(), admin_id=admin.id)
    user.groups = [plain]
    user.data_limit = GIGABYTE
    user.used_traffic = GIGABYTE
    db.add(user)
    await db.flush()
    db.add(NextPlan(user_id=user.id, user_template_id=template.id))
    await db.commit()
    return SimpleNamespace(user_id=user.id)


async def _stored(engine, user_id):
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as session:
        stmt = select(User).where(User.id == user_id).options(selectinload(User.groups))
        user = (await session.execute(stmt)).scalar_one()
        table = ProxyTable.model_validate(user.proxy_settings)
        return SimpleNamespace(
            groups=sorted(group.name for group in user.groups),
            data_limit=user.data_limit,
            used_traffic=user.used_traffic,
            secrets=(table.openvpn.password, table.l2tp.password, table.mtproto.secret),
        )


@pytest.mark.asyncio
async def test_applying_the_next_plan_grants_the_secrets_its_groups_entitle(engine, db):
    seed = await _seed(db)
    db_user = await get_user_by_id(db, seed.user_id)

    await reset_user_by_next(db, db_user)

    stored = await _stored(engine, seed.user_id)
    assert stored.groups == ["vpn"]
    assert stored.data_limit == 5 * GIGABYTE
    assert all(stored.secrets)
    assert len(set(stored.secrets)) == 3


@pytest.mark.asyncio
async def test_a_failed_grant_still_applies_the_plan_and_logs_the_user_and_field(engine, db, monkeypatch, errors_seen):
    seed = await _seed(db)

    async def exhausted(db, spec, count, reserved=None):
        raise ProxySecretUniquenessError([EntitledSecretField.openvpn_password])

    monkeypatch.setattr(entitled_module, "issue_unique_secrets", exhausted)
    db_user = await get_user_by_id(db, seed.user_id)

    await reset_user_by_next(db, db_user)

    stored = await _stored(engine, seed.user_id)
    assert stored.groups == ["vpn"]
    assert stored.data_limit == 5 * GIGABYTE
    assert stored.used_traffic == 0
    assert stored.secrets == (None, None, None)
    messages = [record.getMessage() for record in errors_seen]
    assert any(str(seed.user_id) in message and "openvpn.password" in message for message in messages), messages
