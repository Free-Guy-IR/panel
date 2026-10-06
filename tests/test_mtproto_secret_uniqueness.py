import logging
import re
from dataclasses import replace
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.crud.user import create_user, get_user_by_id
from app.db.models import Admin, AdminRole, Base, CoreConfig, Group, ProxyInbound, User
from app.fork.proxy_secrets import SECRET_FIELD_SPECS, SPEC_BY_FIELD, BulkRepairProxySecrets
from app.models.core import CoreType
from app.models.proxy import ProxyTable
from app.models.user import UserCreate, UserModify
from app.operation import OperatorType
from app.operation.user import UserOperation

MTP = "mtp-443"
HELD = "0123456789abcdef0123456789abcdef"
OWNER = SimpleNamespace(username="owner", is_owner=True, role=None)
HEX32 = re.compile(r"[0-9a-f]{32}")


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
def warnings_seen():
    records = []
    handler = logging.Handler(level=logging.WARNING)
    handler.emit = records.append
    logger = logging.getLogger("proxy-secret-uniqueness")
    logger.addHandler(handler)
    yield records
    logger.removeHandler(handler)


def _operation():
    operation = UserOperation.__new__(UserOperation)
    operation.operator_type = OperatorType.API
    return operation


def _mtproto_spec():
    matches = [spec for spec in SECRET_FIELD_SPECS if spec.field.value == "mtproto.secret"]
    assert matches, "mtproto.secret is not a guarded secret field"
    return matches[0]


async def _seed(db):
    role = AdminRole(name="owner", is_owner=True)
    db.add(role)
    await db.flush()
    admin = Admin(username="owner", hashed_password="x", role_id=role.id)
    db.add(admin)
    inbound = ProxyInbound(tag=MTP)
    db.add(inbound)
    await db.flush()
    db.add(CoreConfig(name="mtproto", type=CoreType.mtproto, config={"instances": [{"tag": MTP}]}))
    group = Group(name="mtproto", inbounds=[inbound])
    db.add(group)
    await db.flush()

    ids = {}
    for username, secret in (("holder", HELD), ("other", "fedcba9876543210fedcba9876543210")):
        settings = ProxyTable().dict()
        settings["mtproto"] = {"secret": secret}
        user = User(username=username, proxy_settings=settings, admin_id=admin.id)
        user.groups = [group]
        db.add(user)
        await db.flush()
        ids[username] = user.id
    await db.commit()
    return SimpleNamespace(admin=admin, group=group, ids=ids)


async def _stored_secret(engine, username):
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as session:
        user = (await session.execute(select(User).where(User.username == username))).scalar_one()
        return user.proxy_settings["mtproto"]["secret"]


def _collision_warnings(records):
    return [record.getMessage() for record in records if "mtproto.secret" in record.getMessage()]


@pytest.mark.asyncio
async def test_a_supplied_mtproto_secret_held_by_another_user_is_replaced_on_create(engine, db, warnings_seen):
    seed = await _seed(db)
    new_user = UserCreate(username="newcomer", proxy_settings={"mtproto": {"secret": HELD}})

    new_user.proxy_settings = await _operation()._prepare_user_proxy_settings(db, [seed.group], new_user.proxy_settings)
    await create_user(db, new_user, [seed.group], seed.admin)

    stored = await _stored_secret(engine, "newcomer")
    assert stored != HELD
    assert HEX32.fullmatch(stored)
    assert await _stored_secret(engine, "holder") == HELD
    assert _collision_warnings(warnings_seen)


@pytest.mark.asyncio
async def test_a_supplied_mtproto_secret_held_by_another_user_is_refused_on_modify(engine, db, warnings_seen):
    seed = await _seed(db)
    db_user = await get_user_by_id(db, seed.ids["other"], load_admin_role=True)
    modified = UserModify(proxy_settings={"mtproto": {"secret": HELD}})

    with pytest.raises(HTTPException) as refused:
        await _operation()._prepare_modified_user(db, db_user, modified, OWNER, skip_role_limits=True)

    assert refused.value.status_code == 409
    assert await _stored_secret(engine, "other") == "fedcba9876543210fedcba9876543210"
    assert await _stored_secret(engine, "holder") == HELD
    assert _collision_warnings(warnings_seen)


@pytest.mark.asyncio
async def test_an_exhausted_mtproto_regeneration_refuses_the_create(engine, db, monkeypatch):
    seed = await _seed(db)
    spec = _mtproto_spec()
    monkeypatch.setitem(SPEC_BY_FIELD, spec.field, replace(spec, generate=lambda: HELD))

    with pytest.raises(HTTPException) as raised:
        await _operation()._prepare_user_proxy_settings(db, [seed.group], ProxyTable(mtproto={"secret": HELD}))

    assert raised.value.status_code == 409
    assert "mtproto.secret" in raised.value.detail


@pytest.mark.asyncio
async def test_the_duplicate_repair_accepts_and_fixes_the_mtproto_secret(engine, db, monkeypatch):
    from app.operation import user as user_module

    seed = await _seed(db)
    other = await db.get(User, seed.ids["other"])
    await db.execute(
        update(User.__table__)
        .where(User.__table__.c.id == other.id)
        .values(proxy_settings={**other.proxy_settings, "mtproto": {"secret": HELD}})
    )
    await db.commit()

    async def no_sync(users):
        return None

    monkeypatch.setattr(user_module, "sync_users", no_sync)
    result = await _operation().bulk_repair_duplicate_proxy_secrets(
        db, BulkRepairProxySecrets(secret_fields={"mtproto.secret"})
    )

    assert result == {"detail": "operation has been successfuly done on 2 users"}
    secrets = {await _stored_secret(engine, "holder"), await _stored_secret(engine, "other")}
    assert HELD not in secrets
    assert len(secrets) == 2
    assert all(HEX32.fullmatch(secret) for secret in secrets)
