from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Admin, Base, User
from app.fork.proxy_secrets import (
    SPEC_BY_FIELD,
    ProxySecretField,
    ProxySecretUniquenessError,
    enforce_unique_proxy_secrets,
    write_secret,
    write_stored_secret,
)
from app.models.proxy import ProxyTable
from app.models.user import UserCreate
from app.operation import OperatorType
from app.operation.user import UserOperation

HY2 = ProxySecretField.hysteria2_password
TAKEN = "a-password-two-users-both-want"


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


def _operation():
    operation = UserOperation.__new__(UserOperation)
    operation.operator_type = OperatorType.API
    return operation


def _coerce(spec, value):
    return UUID(value) if spec.generate is uuid4 else value


def _incoming(values=None):
    proxy_settings = ProxyTable()
    for field, value in (values or {}).items():
        spec = SPEC_BY_FIELD[field]
        write_secret(proxy_settings, spec, _coerce(spec, value))
    return proxy_settings


def _stored(values=None):
    settings = ProxyTable().dict()
    for field, value in (values or {}).items():
        settings = write_stored_secret(settings, SPEC_BY_FIELD[field], value)
    return settings


async def _admin(db):
    admin = Admin(username="owner", hashed_password="x", role_id=1)
    db.add(admin)
    await db.flush()
    return admin


async def _add_user(db, admin, username, values=None):
    user = User(username=username, proxy_settings=_stored(values), admin_id=admin.id)
    db.add(user)
    await db.flush()
    await db.commit()
    return user


def _freeze_generator(monkeypatch, field, value):
    spec = SPEC_BY_FIELD[field]
    monkeypatch.setitem(SPEC_BY_FIELD, field, replace(spec, generate=lambda: value))


@pytest.mark.asyncio
async def test_an_exhausted_guard_raises_instead_of_returning_a_colliding_secret(db, monkeypatch):
    admin = await _admin(db)
    await _add_user(db, admin, "existing", {HY2: TAKEN})
    _freeze_generator(monkeypatch, HY2, TAKEN)

    with pytest.raises(ProxySecretUniquenessError) as raised:
        await enforce_unique_proxy_secrets(db, _incoming({HY2: TAKEN}), fields={HY2})

    assert raised.value.fields == [HY2.value]


@pytest.mark.asyncio
async def test_an_exhausted_guard_refuses_the_create_instead_of_saving_the_duplicate(db, monkeypatch):
    admin = await _admin(db)
    await _add_user(db, admin, "existing", {HY2: TAKEN})
    _freeze_generator(monkeypatch, HY2, TAKEN)

    with pytest.raises(HTTPException) as raised:
        await _operation()._prepare_user_proxy_settings(db, [], _incoming({HY2: TAKEN}))

    assert raised.value.status_code == 409
    assert HY2.value in raised.value.detail


@pytest.mark.asyncio
async def test_a_guard_that_can_still_generate_a_free_value_keeps_working(db, monkeypatch):
    admin = await _admin(db)
    await _add_user(db, admin, "existing", {HY2: TAKEN})

    prepared = await _operation()._prepare_user_proxy_settings(db, [], _incoming({HY2: TAKEN}))

    assert prepared.hysteria2.password
    assert prepared.hysteria2.password != TAKEN


@pytest.mark.asyncio
async def test_a_value_claimed_earlier_in_the_same_request_is_not_handed_out_twice(db):
    await _admin(db)
    reserved: dict = {}

    first = await _operation()._prepare_user_proxy_settings(db, [], _incoming({HY2: TAKEN}), reserved_secrets=reserved)
    second = await _operation()._prepare_user_proxy_settings(db, [], _incoming({HY2: TAKEN}), reserved_secrets=reserved)

    assert first.hysteria2.password == TAKEN
    assert second.hysteria2.password != TAKEN


@pytest.mark.asyncio
async def test_without_a_shared_reservation_the_database_only_check_lets_both_through(db):
    await _admin(db)

    first = await _operation()._prepare_user_proxy_settings(db, [], _incoming({HY2: TAKEN}))
    second = await _operation()._prepare_user_proxy_settings(db, [], _incoming({HY2: TAKEN}))

    assert first.hysteria2.password == TAKEN
    assert second.hysteria2.password == TAKEN


@pytest.mark.asyncio
async def test_a_bulk_create_does_not_persist_two_users_with_the_same_guarded_secret(db, monkeypatch):
    from app.operation import user as user_module

    admin = await _admin(db)
    captured = {}

    async def fake_create_users_bulk(session, users_to_create, groups, db_admin, commit=True):
        captured["users"] = list(users_to_create)
        return []

    monkeypatch.setattr(user_module, "create_users_bulk", fake_create_users_bulk)

    users = [
        UserCreate(username="bulk-one", proxy_settings={"hysteria2": {"password": TAKEN}}),
        UserCreate(username="bulk-two", proxy_settings={"hysteria2": {"password": TAKEN}}),
    ]

    await _operation()._persist_bulk_users(
        db,
        SimpleNamespace(is_owner=True),
        admin,
        users,
        [],
        skip_per_user_limits=True,
        sync=False,
    )

    passwords = [user.proxy_settings.hysteria2.password for user in captured["users"]]
    assert len(passwords) == 2
    assert all(passwords)
    assert len(set(passwords)) == 2


@pytest.mark.asyncio
async def test_a_bulk_create_still_guards_against_users_already_in_the_database(db, monkeypatch):
    from app.operation import user as user_module

    admin = await _admin(db)
    await _add_user(db, admin, "existing", {HY2: TAKEN})
    captured = {}

    async def fake_create_users_bulk(session, users_to_create, groups, db_admin, commit=True):
        captured["users"] = list(users_to_create)
        return []

    monkeypatch.setattr(user_module, "create_users_bulk", fake_create_users_bulk)

    users = [UserCreate(username="bulk-one", proxy_settings={"hysteria2": {"password": TAKEN}})]

    await _operation()._persist_bulk_users(
        db,
        SimpleNamespace(is_owner=True),
        admin,
        users,
        [],
        skip_per_user_limits=True,
        sync=False,
    )

    stored = (await db.execute(select(User.username))).scalars().all()
    assert stored == ["existing"]
    assert captured["users"][0].proxy_settings.hysteria2.password != TAKEN
