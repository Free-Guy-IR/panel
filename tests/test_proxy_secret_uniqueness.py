from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Admin, Base, User
from app.fork.proxy_secrets import (
    SECRET_FIELD_SPECS,
    SPEC_BY_FIELD,
    BulkRepairProxySecrets,
    ProxySecretField,
    read_stored_secret,
    write_secret,
    write_stored_secret,
)
from app.models.proxy import ProxyTable
from app.operation import OperatorType
from app.operation.user import UserOperation

SHARED = {
    ProxySecretField.hysteria_auth: "shared-hysteria-auth-value",
    ProxySecretField.hysteria2_password: "shared-hysteria2-password-value",
    ProxySecretField.tuic_password: "shared-tuic-password-value",
    ProxySecretField.tuic_uuid: "11111111-1111-4111-8111-111111111111",
    ProxySecretField.trojan_password: "shared-trojan-password-value",
    ProxySecretField.shadowsocks_password: "shared-shadowsocks-password-value",
    ProxySecretField.vless_id: "22222222-2222-4222-8222-222222222222",
    ProxySecretField.vmess_id: "33333333-3333-4333-8333-333333333333",
}

HY2 = ProxySecretField.hysteria2_password
HY1 = ProxySecretField.hysteria_auth


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def synced(monkeypatch):
    from app.operation import user as user_module

    captured = {"users": None}

    async def fake_sync(users):
        captured["users"] = list(users)

    monkeypatch.setattr(user_module, "sync_users", fake_sync)
    return captured


def _operation():
    operation = UserOperation.__new__(UserOperation)
    operation.operator_type = OperatorType.API
    return operation


def _coerce(spec, value):
    return UUID(value) if spec.generate is uuid4 else value


def _stored(values=None):
    settings = ProxyTable().dict()
    for field, value in (values or {}).items():
        settings = write_stored_secret(settings, SPEC_BY_FIELD[field], value)
    return settings


def _incoming(values=None):
    proxy_settings = ProxyTable()
    for field, value in (values or {}).items():
        spec = SPEC_BY_FIELD[field]
        write_secret(proxy_settings, spec, _coerce(spec, value))
    return proxy_settings


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


async def _reload(db):
    result = await db.execute(select(User))
    return {user.username: user for user in result.scalars().all()}


def _value(user, field):
    return read_stored_secret(user.proxy_settings, SPEC_BY_FIELD[field])


@pytest.mark.asyncio
async def test_a_supplied_secret_that_another_user_holds_is_replaced_on_create(db):
    admin = await _admin(db)
    await _add_user(db, admin, "existing", {HY2: SHARED[HY2]})

    prepared = await _operation()._prepare_user_proxy_settings(db, [], _incoming({HY2: SHARED[HY2]}))

    assert prepared.hysteria2.password
    assert prepared.hysteria2.password != SHARED[HY2]


@pytest.mark.asyncio
async def test_every_secret_bearing_field_is_guarded_on_create(db):
    admin = await _admin(db)
    await _add_user(db, admin, "existing", SHARED)

    prepared = await _operation()._prepare_user_proxy_settings(db, [], _incoming(SHARED))

    survived = [
        spec.field.value
        for spec in SECRET_FIELD_SPECS
        if str(getattr(getattr(prepared, spec.protocol), spec.attribute)) == SHARED[spec.field]
    ]
    assert not survived


@pytest.mark.asyncio
async def test_a_unique_supplied_secret_is_left_alone_on_create(db):
    admin = await _admin(db)
    await _add_user(db, admin, "existing", SHARED)
    mine = {field: f"mine-{value}" for field, value in SHARED.items() if SPEC_BY_FIELD[field].generate is not uuid4}

    prepared = await _operation()._prepare_user_proxy_settings(db, [], _incoming(mine))

    for field, value in mine.items():
        spec = SPEC_BY_FIELD[field]
        assert str(getattr(getattr(prepared, spec.protocol), spec.attribute)) == value


@pytest.mark.asyncio
async def test_a_supplied_secret_that_another_user_holds_is_replaced_on_update(db):
    admin = await _admin(db)
    await _add_user(db, admin, "existing", {HY2: SHARED[HY2]})
    target = await _add_user(db, admin, "target", {HY2: "target-own-password"})

    prepared = await _operation()._prepare_user_proxy_settings(
        db, [], _incoming({HY2: SHARED[HY2]}), exclude_user_id=target.id
    )

    assert prepared.hysteria2.password
    assert prepared.hysteria2.password != SHARED[HY2]


@pytest.mark.asyncio
async def test_resaving_a_user_unchanged_is_not_a_collision_with_itself(db):
    admin = await _admin(db)
    target = await _add_user(db, admin, "target", SHARED)

    prepared = await _operation()._prepare_user_proxy_settings(db, [], _incoming(SHARED), exclude_user_id=target.id)

    for spec in SECRET_FIELD_SPECS:
        assert str(getattr(getattr(prepared, spec.protocol), spec.attribute)) == SHARED[spec.field]


@pytest.mark.asyncio
async def test_the_same_resave_without_the_exclusion_would_be_replaced(db):
    admin = await _admin(db)
    await _add_user(db, admin, "target", SHARED)

    prepared = await _operation()._prepare_user_proxy_settings(db, [], _incoming(SHARED))

    for spec in SECRET_FIELD_SPECS:
        assert str(getattr(getattr(prepared, spec.protocol), spec.attribute)) != SHARED[spec.field]


@pytest.mark.asyncio
async def test_repair_gives_every_member_of_a_duplicate_group_a_fresh_unique_secret(db, synced):
    admin = await _admin(db)
    for name in ("bot-one", "bot-two", "bot-three"):
        await _add_user(db, admin, name, {HY2: SHARED[HY2]})
    await _add_user(db, admin, "honest", {HY2: "honest-own-password"})

    result = await _operation().bulk_repair_duplicate_proxy_secrets(db, BulkRepairProxySecrets(secret_fields={HY2}))

    assert result == {"detail": "operation has been successfuly done on 3 users"}
    assert sorted(user.username for user in synced["users"]) == ["bot-one", "bot-three", "bot-two"]

    rows = await _reload(db)
    values = [_value(user, HY2) for user in rows.values()]
    assert len(set(values)) == len(values)
    assert SHARED[HY2] not in values
    assert _value(rows["honest"], HY2) == "honest-own-password"


@pytest.mark.asyncio
async def test_repair_dry_run_changes_nothing(db, synced):
    admin = await _admin(db)
    for name in ("bot-one", "bot-two"):
        await _add_user(db, admin, name, {HY2: SHARED[HY2]})

    result = await _operation().bulk_repair_duplicate_proxy_secrets(
        db, BulkRepairProxySecrets(secret_fields={HY2}, dry_run=True)
    )

    assert result.affected_users == 2
    assert synced["users"] is None
    rows = await _reload(db)
    assert [_value(user, HY2) for user in rows.values()] == [SHARED[HY2], SHARED[HY2]]


@pytest.mark.asyncio
async def test_repair_touches_only_the_requested_protocol(db, synced):
    admin = await _admin(db)
    for name in ("bot-one", "bot-two"):
        await _add_user(db, admin, name, {HY2: SHARED[HY2], HY1: SHARED[HY1]})

    await _operation().bulk_repair_duplicate_proxy_secrets(db, BulkRepairProxySecrets(secret_fields={HY2}))

    rows = await _reload(db)
    assert len({_value(user, HY2) for user in rows.values()}) == 2
    assert {_value(user, HY1) for user in rows.values()} == {SHARED[HY1]}


@pytest.mark.asyncio
async def test_repair_reports_zero_when_nothing_is_shared(db, synced):
    admin = await _admin(db)
    await _add_user(db, admin, "one", {HY2: "one-own-password"})
    await _add_user(db, admin, "two", {HY2: "two-own-password"})

    result = await _operation().bulk_repair_duplicate_proxy_secrets(db, BulkRepairProxySecrets(secret_fields={HY2}))

    assert result == {"detail": "operation has been successfuly done on 0 users"}
    assert synced["users"] is None


@pytest.mark.asyncio
async def test_repair_honours_the_user_filter(db, synced):
    admin = await _admin(db)
    first = await _add_user(db, admin, "bot-one", {HY2: SHARED[HY2]})
    await _add_user(db, admin, "bot-two", {HY2: SHARED[HY2]})

    result = await _operation().bulk_repair_duplicate_proxy_secrets(
        db, BulkRepairProxySecrets(secret_fields={HY2}, users={first.id})
    )

    assert result == {"detail": "operation has been successfuly done on 1 users"}
    rows = await _reload(db)
    assert _value(rows["bot-one"], HY2) != SHARED[HY2]
    assert _value(rows["bot-two"], HY2) == SHARED[HY2]


def test_repair_requires_an_explicit_protocol():
    with pytest.raises(ValidationError):
        BulkRepairProxySecrets()
    with pytest.raises(ValidationError):
        BulkRepairProxySecrets(secret_fields=set())
